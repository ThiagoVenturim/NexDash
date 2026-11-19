"""Building blocks shared by the ready-made report models.

A report model is one cube, the table behind it, and a dashboard template made of
panels. Each panel is a ``QueryPlan`` against the cube plus a visualization (KPI,
line or horizontal bar). Runtime parameters (period, team, project) are bound
through ``bind_runtime_parameters`` from the reports module, so every value ends up
as a bound SQL parameter and the guarded compiler checks every field against the
cube allowlist.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable, DropTable

from nexdash.compiler import SQLCompiler
from nexdash.cube import Cube, CubeRegistry, validate_identifier
from nexdash.executor import DatabaseExecutor, SQLiteExecutor
from nexdash.plan import PlanFilter, QueryPlan
from nexdash.reports import (
    ComputedChart,
    ReportChart,
    ReportParameter,
    ReportSpec,
    bind_runtime_parameters,
    compute_charts_from_rows,
)

PanelKind = Literal["kpi", "line", "bar"]
Unit = Literal["count", "hours", "minutes", "percent", "score"]

MAX_KPIS = 5
MAX_PARAM_CHARS = 200
SNAPSHOT_METRIC = "latest_snapshot"

_CHART_TYPES: dict[str, str] = {"kpi": "kpi", "line": "line", "bar": "bar"}


class ReportModelError(ValueError):
    """Raised when a report model definition is inconsistent with its cube."""


class ModelParameters(BaseModel):
    """Runtime parameters every report model accepts: period, team and project.

    ``start`` and ``end`` are inclusive dates. Omitted values mean "no filter".
    """

    model_config = ConfigDict(extra="forbid")

    start: dt.date | None = None
    end: dt.date | None = None
    team: str | None = None
    project: str | None = None

    @field_validator("team", "project", mode="before")
    @classmethod
    def _clean_text(cls, v: Any) -> str | None:
        if v is None:
            return None
        text = str(v).strip()
        if not text:
            return None
        if len(text) > MAX_PARAM_CHARS:
            raise ValueError(f"Parameter value is longer than {MAX_PARAM_CHARS} characters")
        return text

    @model_validator(mode="after")
    def _check_period(self) -> ModelParameters:
        if self.start and self.end and self.end < self.start:
            raise ValueError("Parameter 'end' must not be earlier than 'start'")
        return self

    def previous_period(self) -> ModelParameters | None:
        """Return the period of the same length right before this one, or None without both bounds."""
        if self.start is None or self.end is None:
            return None
        length = (self.end - self.start).days + 1
        prev_end = self.start - dt.timedelta(days=1)
        prev_start = prev_end - dt.timedelta(days=length - 1)
        return self.model_copy(update={"start": prev_start, "end": prev_end})

    def describe(self) -> str:
        """Human-readable filter state, for example ``Period: 2026-01-01 to 2026-06-30 | Team: all``."""
        if self.start and self.end:
            period = f"{self.start.isoformat()} to {self.end.isoformat()}"
        elif self.start:
            period = f"from {self.start.isoformat()}"
        elif self.end:
            period = f"until {self.end.isoformat()}"
        else:
            period = "all time"
        return f"Period: {period} | Team: {self.team or 'all'} | Project: {self.project or 'all'}"


@dataclass(frozen=True)
class Panel:
    """One visualization of a report model.

    ``title`` says what is measured (it becomes the chart subtitle); the headline is
    computed from the data when the dashboard is rendered.
    """

    id: str
    kind: PanelKind
    title: str
    plan: QueryPlan
    y: str
    x: str | None = None
    unit: Unit = "count"
    period_field: str | None = None
    latest_snapshot: bool = False
    keep_order: bool = False
    highlight: str | None = None
    lower_is_better: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        validate_identifier(self.id, "panel id")
        if self.kind in ("line", "bar") and not self.x:
            raise ReportModelError(f"Panel '{self.id}' of kind '{self.kind}' needs an 'x' field")
        if self.y not in self.plan.metrics:
            raise ReportModelError(f"Panel '{self.id}' plots '{self.y}', which its plan does not request")
        if self.x and self.x not in self.plan.dimensions:
            raise ReportModelError(f"Panel '{self.id}' uses '{self.x}' as x, which its plan does not group by")


@dataclass(frozen=True)
class PanelResult:
    """Rows and computed chart series of one panel."""

    panel: Panel
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    chart: ComputedChart | None = None
    previous_value: Any = None
    snapshot: str | None = None
    error: str | None = None

    @property
    def value(self) -> Any:
        """KPI value (first row of the metric), or None for charts and empty results."""
        if self.chart is None:
            return None
        return self.chart.kpi_value

    @property
    def is_empty(self) -> bool:
        if self.error:
            return False
        if self.panel.kind == "kpi":
            return self.value is None
        return not self.rows


@dataclass(frozen=True)
class ModelRun:
    """The outcome of running every panel of a report model with one set of parameters."""

    model: ReportModel
    params: ModelParameters
    generated_at: str
    panels: list[PanelResult]

    @property
    def kpis(self) -> list[PanelResult]:
        return [p for p in self.panels if p.panel.kind == "kpi"]

    @property
    def charts(self) -> list[PanelResult]:
        return [p for p in self.panels if p.panel.kind != "kpi"]

    @property
    def errors(self) -> dict[str, str]:
        return {p.panel.id: p.error for p in self.panels if p.error}

    def panel(self, panel_id: str) -> PanelResult:
        for result in self.panels:
            if result.panel.id == panel_id:
                return result
        raise KeyError(f"Panel '{panel_id}' not found in model '{self.model.name}'")

    def dataset(self) -> dict[str, Any]:
        """JSON-friendly data of every panel, keyed by panel id."""
        return {
            p.panel.id: {
                "kind": p.panel.kind,
                "title": p.panel.title,
                "unit": p.panel.unit,
                "columns": list(p.columns),
                "rows": [dict(row) for row in p.rows],
                "value": p.value,
                "previous_value": p.previous_value,
                "snapshot": p.snapshot,
                "error": p.error,
            }
            for p in self.panels
        }


def _iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class ReportModel:
    """A ready-made report: one cube, its table and a dashboard template made of panels."""

    name: str
    title: str
    description: str
    cube: Cube
    table: sa.Table
    panels: tuple[Panel, ...]
    period_field: str
    snapshot_field: str | None = None
    source_note: str = ""

    def __post_init__(self) -> None:
        validate_identifier(self.name, "report model name")
        if self.table.name != self.cube.source:
            raise ReportModelError(f"Model '{self.name}': table '{self.table.name}' is not the cube source")
        dims = self.cube.allowed_dimension_names
        required = {self.period_field, "team", "project"}
        if self.snapshot_field:
            required.add(self.snapshot_field)
            if SNAPSHOT_METRIC not in self.cube.allowed_metric_names:
                raise ReportModelError(f"Model '{self.name}' needs a '{SNAPSHOT_METRIC}' metric for snapshots")
        missing = sorted(required - dims)
        if missing:
            raise ReportModelError(f"Model '{self.name}': cube lacks dimensions {missing}")
        columns = set(self.table.columns.keys())
        for dim in self.cube.dimensions:
            if dim.column not in columns:
                raise ReportModelError(f"Model '{self.name}': dimension '{dim.name}' has no table column")
        ids = [p.id for p in self.panels]
        if len(ids) != len(set(ids)):
            raise ReportModelError(f"Model '{self.name}' has duplicate panel ids")
        if sum(1 for p in self.panels if p.kind == "kpi") > MAX_KPIS:
            raise ReportModelError(f"Model '{self.name}' has more than {MAX_KPIS} KPIs")
        compiler = SQLCompiler(default_dialect="sqlite")
        for panel in self.panels:
            if panel.plan.cube != self.cube.name:
                raise ReportModelError(f"Panel '{panel.id}' queries cube '{panel.plan.cube}', not '{self.cube.name}'")
            if panel.latest_snapshot and not self.snapshot_field:
                raise ReportModelError(f"Panel '{panel.id}' reads the latest snapshot but the model has none")
            period = panel.period_field or self.period_field
            if period not in dims:
                raise ReportModelError(f"Panel '{panel.id}': period field '{period}' is not a dimension")
            # Compiling once proves every metric, dimension and filter is in the allowlist.
            compiler.compile(self._bind(panel.plan, ModelParameters(team="x", project="x"), period), self.cube)

    # -- definition helpers ------------------------------------------------

    def report_spec(self, panel: Panel) -> ReportSpec:
        """The panel as a managed ``ReportSpec`` (usable with ``ReportStore``)."""
        return ReportSpec(
            title=f"{self.title}: {panel.title}",
            description=panel.description or None,
            plan=panel.plan,
            parameters=[
                ReportParameter(name="team", field="team", description="Team name"),
                ReportParameter(name="project", field="project", description="Project or repository"),
            ],
            charts=[ReportChart(id=panel.id, type=_CHART_TYPES[panel.kind], title=panel.title, x=panel.x, y=panel.y)],
            tags=["report-model", self.name],
        )

    def report_specs(self) -> dict[str, ReportSpec]:
        """Every panel as a ``ReportSpec``, keyed by ``<model>_<panel>``."""
        return {f"{self.name}_{panel.id}": self.report_spec(panel) for panel in self.panels}

    def registry(self) -> CubeRegistry:
        return CubeRegistry({self.cube.name: self.cube})

    def _bind(
        self,
        plan: QueryPlan,
        params: ModelParameters,
        period_field: str,
        snapshot: str | None = None,
    ) -> QueryPlan:
        spec = ReportSpec(
            title=self.title,
            plan=plan,
            parameters=[ReportParameter(name="team", field="team"), ReportParameter(name="project", field="project")],
        )
        runtime: dict[str, Any] = {"team": params.team, "project": params.project}
        if params.start or params.end:
            runtime["time_range"] = {"field": period_field, "start": _iso(params.start), "end": _iso(params.end)}
        bound = bind_runtime_parameters(spec, runtime)
        if snapshot is not None and self.snapshot_field:
            filters = [*bound.filters, PlanFilter(field=self.snapshot_field, operator="eq", value=snapshot)]
            bound = bound.model_copy(update={"filters": filters})
        return bound

    def bind(self, panel: Panel, params: ModelParameters | None = None, snapshot: str | None = None) -> QueryPlan:
        """Return the panel plan with period, team, project (and snapshot) filters bound."""
        return self._bind(panel.plan, params or ModelParameters(), panel.period_field or self.period_field, snapshot)

    # -- data --------------------------------------------------------------

    def load_rows(
        self,
        executor: DatabaseExecutor | SQLiteExecutor,
        rows: Iterable[Mapping[str, Any]],
        *,
        replace: bool = True,
    ) -> int:
        """Write rows into the model table with bound parameters. Returns the number of rows written.

        With ``replace=True`` the table is dropped and created again, so a refresh does not duplicate rows.
        """
        names = list(self.table.columns.keys())
        records = [{name: row.get(name) for name in names} for row in rows]
        if isinstance(executor, SQLiteExecutor):
            dialect = sqlite.dialect()
            conn = executor.connection
            if replace:
                conn.execute(str(DropTable(self.table, if_exists=True).compile(dialect=dialect)))
            conn.execute(str(CreateTable(self.table, if_not_exists=True).compile(dialect=dialect)))
            if records:
                insert_sql = str(self.table.insert().compile(dialect=sqlite.dialect(paramstyle="named")))
                conn.executemany(insert_sql, records)
            conn.commit()
            return len(records)
        with executor.engine.begin() as conn:
            if replace:
                self.table.drop(conn, checkfirst=True)
            self.table.create(conn, checkfirst=True)
            if records:
                conn.execute(self.table.insert(), records)
        return len(records)

    def latest_snapshot(
        self,
        executor: DatabaseExecutor | SQLiteExecutor,
        params: ModelParameters | None = None,
    ) -> str | None:
        """Most recent snapshot value inside the period, team and project filters."""
        if not self.snapshot_field:
            return None
        plan = QueryPlan(cube=self.cube.name, metrics=[SNAPSHOT_METRIC])
        bound = self._bind(plan, params or ModelParameters(), self.snapshot_field)
        result = executor.execute_plan(bound, self.cube)
        value = result.rows[0].get(SNAPSHOT_METRIC) if result.rows else None
        return None if value is None else str(value)

    def _run_panel(
        self,
        executor: DatabaseExecutor | SQLiteExecutor,
        panel: Panel,
        params: ModelParameters,
        snapshot: str | None,
    ) -> tuple[list[str], list[dict[str, Any]], ComputedChart]:
        plan = self.bind(panel, params, snapshot if panel.latest_snapshot else None)
        result = executor.execute_plan(plan, self.cube)
        charts, _warnings = compute_charts_from_rows(self.report_spec(panel), result.columns, result.rows)
        return list(result.columns), list(result.rows), charts[0]

    def run(
        self,
        executor: DatabaseExecutor | SQLiteExecutor,
        params: ModelParameters | Mapping[str, Any] | None = None,
        *,
        compare: bool = True,
        now: dt.datetime | None = None,
    ) -> ModelRun:
        """Run every panel. Failures are kept per panel so the dashboard can show an error state.

        With ``compare=True`` and both period bounds set, KPIs are also computed for the
        previous period of the same length.
        """
        active = params if isinstance(params, ModelParameters) else ModelParameters.model_validate(params or {})
        previous = active.previous_period() if compare else None
        snapshot = self.latest_snapshot(executor, active) if self.snapshot_field else None
        prev_snapshot = (
            self.latest_snapshot(executor, previous) if self.snapshot_field and previous is not None else None
        )

        results: list[PanelResult] = []
        for panel in self.panels:
            if panel.latest_snapshot and snapshot is None and self.snapshot_field:
                results.append(PanelResult(panel=panel, snapshot=None))
                continue
            try:
                columns, rows, chart = self._run_panel(executor, panel, active, snapshot)
            except Exception as exc:  # shown as the panel error state
                results.append(PanelResult(panel=panel, error=f"{type(exc).__name__}: {exc}"[:300]))
                continue
            previous_value = None
            if panel.kind == "kpi" and previous is not None and not (panel.latest_snapshot and prev_snapshot is None):
                try:
                    _cols, _rows, prev_chart = self._run_panel(executor, panel, previous, prev_snapshot)
                    previous_value = prev_chart.kpi_value
                except Exception:  # the comparison is optional; the KPI still renders
                    previous_value = None
            results.append(
                PanelResult(
                    panel=panel,
                    columns=columns,
                    rows=rows,
                    chart=chart,
                    previous_value=previous_value,
                    snapshot=snapshot if panel.latest_snapshot else None,
                )
            )

        generated = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        return ModelRun(model=self, params=active, generated_at=generated, panels=results)
