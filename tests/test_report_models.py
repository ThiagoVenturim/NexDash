"""Offline tests for the report models (dev productivity, security posture, support metrics).

Everything runs on deterministic synthetic data in in-memory SQLite databases.
No network, no model calls.
"""

import datetime as dt
import json
import re

import pytest
from pydantic import BaseModel, ValidationError

from nexdash import DashboardStore, DatabaseExecutor, PlanFilter, QueryPlan, ReportStore, SQLiteExecutor
from nexdash.connectors import GitHubConnector, WorkItem
from nexdash.report_models import (
    REPORT_MODELS,
    ModelParameters,
    Panel,
    ReportModel,
    ReportModelError,
    compare_text,
    dev_productivity,
    dev_rows_from_work_items,
    format_number,
    get_model,
    headline,
    period_start,
    publish_dashboard,
    register_model_cubes,
    render_dashboard,
    security_posture,
    security_rows_from_snapshots,
    support_rows_from_tickets,
)
from nexdash.report_models.base import PanelResult
from nexdash.report_models.synthetic import (
    TEAM_MEMBERS,
    generate_model_rows,
    generate_security_snapshots,
    seed_model_data,
)
from nexdash.reports import ComputedChart
from nexdash.server import build_service

NOW = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
Q2 = ModelParameters(start=dt.date(2026, 4, 1), end=dt.date(2026, 6, 30))
MODEL_NAMES = ("dev_productivity", "security_posture", "support_metrics")


@pytest.fixture(params=["sqlite3", "sqlalchemy"])
def executor(request):
    ex = SQLiteExecutor(":memory:") if request.param == "sqlite3" else DatabaseExecutor("sqlite:///:memory:")
    seed_model_data(ex)
    yield ex
    ex.close()


@pytest.fixture
def sqlite_executor():
    ex = SQLiteExecutor(":memory:")
    seed_model_data(ex)
    yield ex
    ex.close()


# Registry and definitions


def test_three_models_are_registered():
    assert sorted(REPORT_MODELS) == sorted(MODEL_NAMES)
    for name in MODEL_NAMES:
        model = get_model(name)
        assert model.cube.name == name
        assert model.table.name == model.cube.source
        kinds = {panel.kind for panel in model.panels}
        assert kinds == {"kpi", "line", "bar"}
        assert 3 <= len([p for p in model.panels if p.kind == "kpi"]) <= 5
    with pytest.raises(KeyError, match="Available"):
        get_model("nope")


def test_every_model_accepts_period_team_and_project():
    for model in REPORT_MODELS.values():
        dims = model.cube.allowed_dimension_names
        assert {"team", "project", model.period_field} <= dims
        for spec in model.report_specs().values():
            assert [p.name for p in spec.parameters] == ["team", "project"]


def test_model_rejects_panel_outside_allowlist():
    with pytest.raises(ReportModelError, match="does not request"):
        Panel(id="bad", kind="kpi", title="x", plan=QueryPlan(cube="dev_productivity", metrics=["item_count"]), y="z")
    bad_panel = Panel(
        id="bad",
        kind="kpi",
        title="x",
        plan=QueryPlan(cube="dev_productivity", metrics=["item_count"], filters=[PlanFilter(field="secret", value=1)]),
        y="item_count",
    )
    with pytest.raises(Exception, match="secret"):
        ReportModel(
            name="broken",
            title="Broken",
            description="",
            cube=dev_productivity.CUBE,
            table=dev_productivity.TABLE,
            panels=(bad_panel,),
            period_field="created_date",
        )


def test_model_rejects_more_than_five_kpis():
    kpi = dev_productivity.PANELS[0]
    panels = tuple(Panel(**{**kpi.__dict__, "id": f"k{i}"}) for i in range(6))
    with pytest.raises(ReportModelError, match="more than 5 KPIs"):
        ReportModel(
            name="too_many",
            title="Too many",
            description="",
            cube=dev_productivity.CUBE,
            table=dev_productivity.TABLE,
            panels=panels,
            period_field="created_date",
        )


# Parameters


def test_model_parameters_validation_and_previous_period():
    params = ModelParameters(start="2026-04-01", end="2026-06-30", team="  apps ", project="")
    assert params.team == "apps"
    assert params.project is None
    previous = params.previous_period()
    assert previous.start == dt.date(2025, 12, 31)  # same length: 91 days
    assert previous.end == dt.date(2026, 3, 31)
    assert ModelParameters(start="2026-01-01").previous_period() is None
    assert params.describe() == "Period: 2026-04-01 to 2026-06-30 | Team: apps | Project: all"
    with pytest.raises(ValidationError):
        ModelParameters(start="2026-06-01", end="2026-05-01")
    with pytest.raises(ValidationError):
        ModelParameters(sql="DROP TABLE x")
    with pytest.raises(ValidationError):
        ModelParameters(team="x" * 201)


# Each model generates its dashboard from synthetic data


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_each_model_generates_dashboard_offline(executor, name):
    model = get_model(name)
    run = model.run(executor, Q2, now=NOW)
    assert run.errors == {}
    for result in run.panels:
        assert not result.is_empty, result.panel.id
    for kpi in run.kpis:
        assert kpi.previous_value is not None, kpi.panel.id

    html = render_dashboard(run)
    assert html.startswith("<!doctype html>")
    assert "<script" not in html.lower()
    assert "http://" not in html.replace('xmlns="http://www.w3.org/2000/svg"', "")
    for panel in model.panels:
        assert f'id="panel-{panel.id}"' in html
    assert html.count("<svg") == len(run.charts)
    assert html.count("<details>") == len(run.charts)
    assert "Period: 2026-04-01 to 2026-06-30 | Team: all | Project: all" in html
    assert "Generated 2026-07-01 12:00 UTC" in html
    assert "No data" not in html
    json.dumps(run.dataset(), default=str)


def test_synthetic_rows_are_deterministic():
    assert generate_model_rows(3) == generate_model_rows(3)
    assert generate_model_rows(3) != generate_model_rows(4)
    counts = {name: len(rows) for name, rows in generate_model_rows().items()}
    assert counts == {"dev_productivity": 400, "security_posture": 120, "support_metrics": 320}


def test_both_executors_give_the_same_numbers():
    results = []
    for ex in (SQLiteExecutor(":memory:"), DatabaseExecutor("sqlite:///:memory:")):
        with ex:
            seed_model_data(ex)
            results.append({n: get_model(n).run(ex, Q2, now=NOW).dataset() for n in MODEL_NAMES})
    assert results[0] == results[1]


# Dev productivity


def test_dev_team_and_project_filters(sqlite_executor):
    model = get_model("dev_productivity")
    everyone = model.run(sqlite_executor, Q2, now=NOW)
    apps = model.run(sqlite_executor, Q2.model_copy(update={"team": "apps"}), now=NOW)

    people = {row["person"] for row in apps.panel("throughput_person").rows}
    assert people and people <= set(TEAM_MEMBERS["apps"])
    assert [row["team"] for row in apps.panel("throughput_team").rows] == ["apps"]
    assert apps.panel("prs_merged").value < everyone.panel("prs_merged").value

    api = model.run(sqlite_executor, Q2.model_copy(update={"project": "example/api"}), now=NOW)
    web = model.run(sqlite_executor, Q2.model_copy(update={"project": "example/web"}), now=NOW)
    assert api.panel("prs_merged").value + web.panel("prs_merged").value == everyone.panel("prs_merged").value


def test_dev_period_filter_limits_months(sqlite_executor):
    model = get_model("dev_productivity")
    run = model.run(sqlite_executor, ModelParameters(start="2026-05-01", end="2026-05-31"), now=NOW)
    assert [row["closed_month"] for row in run.panel("merged_trend").rows] == ["2026-05"]
    whole = model.run(sqlite_executor, ModelParameters(), now=NOW)
    months = [row["closed_month"] for row in whole.panel("merged_trend").rows]
    assert months == sorted(months)
    assert len(months) >= 6
    assert whole.panel("prs_merged").previous_value is None


def test_dev_numbers_match_the_rows(sqlite_executor):
    rows = generate_model_rows()["dev_productivity"]
    merged_q2 = [
        r
        for r in rows
        if r["item_type"] == "pull_request"
        and r["merged"] == "yes"
        and "2026-04-01" <= r["closed_date"] <= "2026-06-30"
    ]
    run = get_model("dev_productivity").run(sqlite_executor, Q2, now=NOW)
    assert run.panel("prs_merged").value == len(merged_q2)
    expected_lead = sum(r["lead_time_hours"] for r in merged_q2) / len(merged_q2)
    assert run.panel("lead_time").value == pytest.approx(expected_lead)
    throughput = run.panel("throughput_person").rows
    assert [r["item_count"] for r in throughput] == sorted((r["item_count"] for r in throughput), reverse=True)


def test_dev_rows_from_connector_work_items():
    raw_pr = {
        "id": 9,
        "number": 3,
        "title": "Add cache",
        "state": "closed",
        "user": {"login": "alice"},
        "created_at": "2026-02-01T10:00:00Z",
        "closed_at": "2026-02-02T10:00:00Z",
        "pull_request": {},
        "labels": [],
    }
    raw_issue = {
        "id": 10,
        "number": 4,
        "title": "Bug",
        "state": "closed",
        "user": {"login": "bob"},
        "assignee": {"login": "carla"},
        "created_at": "2026-02-01T00:00:00Z",
        "closed_at": "2026-02-03T00:00:00Z",
        "labels": [{"name": "bug"}],
    }
    with GitHubConnector("example", "app") as github:
        items = [github.normalize(raw_pr), github.normalize(raw_issue)]
    open_pr = WorkItem(source="azure_devops", item_id="7", item_type="Pull Request", author="dan", status="active")
    ado_pr = WorkItem(
        source="azure_devops",
        item_id="8",
        item_type="pull request",
        author="erin",
        status="completed",
        created_at="2026-03-01T00:00:00Z",
        closed_at="2026-03-01T06:00:00Z",
    )
    rows = dev_rows_from_work_items(
        [*items, open_pr, ado_pr],
        team_of={"alice": "platform", "carla": "apps"},
        review_hours={"app#3": 2.5},
        merged_keys=["app#3"],
    )
    pr, issue, still_open, completed = rows
    assert pr["item_type"] == "pull_request" and pr["merged"] == "yes" and pr["delivered"] == "yes"
    assert pr["person"] == "alice" and pr["team"] == "platform"
    assert pr["review_hours"] == 2.5 and pr["lead_time_hours"] == 24.0
    assert pr["closed_month"] == "2026-02"
    assert issue["item_type"] == "issue" and issue["person"] == "carla" and issue["team"] == "apps"
    assert issue["delivered"] == "yes" and issue["merged"] == "no" and issue["review_hours"] is None
    assert still_open["state"] == "open" and still_open["delivered"] == "no" and still_open["team"] == "unassigned"
    assert completed["merged"] == "yes" and completed["lead_time_hours"] == 6.0

    unmerged = dev_rows_from_work_items([items[0]])
    assert unmerged[0]["merged"] == "no"


# Security posture


def test_security_current_panels_use_latest_snapshot(sqlite_executor):
    rows = generate_model_rows()["security_posture"]
    run = get_model("security_posture").run(sqlite_executor, Q2, now=NOW)
    assert run.panel("open_total").snapshot == "2026-06-01"
    june = [r for r in rows if r["snapshot_date"] == "2026-06-01"]
    assert run.panel("open_total").value == sum(r["open_findings"] for r in june)
    severe = sum(r["open_findings"] for r in june if r["severity"] in ("critical", "high"))
    assert run.panel("severe_open").value == severe
    march = [r for r in rows if r["snapshot_date"] == "2026-03-01"]
    assert run.panel("open_total").previous_value == sum(r["open_findings"] for r in march)
    assert [r["severity"] for r in run.panel("by_severity").rows] == list(security_posture.SEVERITIES)
    assert [r["snapshot_date"] for r in run.panel("severe_trend").rows] == ["2026-04-01", "2026-05-01", "2026-06-01"]
    assert 0.0 <= run.panel("gate_coverage").value <= 1.0


def test_security_dashboard_keeps_severity_order_and_highlights_critical(sqlite_executor):
    run = get_model("security_posture").run(sqlite_executor, Q2, now=NOW)
    html = render_dashboard(run)
    section = html.split('id="panel-by_severity"')[1].split("</section>")[0]
    labels = re.findall(r'text-anchor="end" font-size="12" fill="#1A1A1A">([a-z]+)</text>', section)
    assert labels == list(security_posture.SEVERITIES)
    assert section.count('fill="#1F5FAD"') == 1
    assert headline(run.panel("by_severity")).startswith("critical: ")


def test_security_team_filter_and_empty_state(sqlite_executor):
    model = get_model("security_posture")
    data = model.run(sqlite_executor, Q2.model_copy(update={"team": "data"}), now=NOW)
    assert [r["project"] for r in data.panel("severe_by_project").rows] == ["example/pipeline"]
    nobody = model.run(sqlite_executor, Q2.model_copy(update={"team": "nobody"}), now=NOW)
    assert all(result.is_empty for result in nobody.panels)
    html = render_dashboard(nobody)
    assert html.count("No data for the selected filters.") == len(model.panels)


def test_period_start_formats():
    assert period_start("2026-03") == dt.date(2026, 3, 1)
    assert period_start("2026-W12") == dt.date(2026, 3, 16)
    assert period_start("2026-Q2") == dt.date(2026, 4, 1)
    assert period_start("2026-03-15T10:00:00Z") == dt.date(2026, 3, 15)
    assert period_start(dt.date(2026, 1, 2)) == dt.date(2026, 1, 2)
    with pytest.raises(ValueError, match="Unrecognized period"):
        period_start("last week")


def test_security_rows_from_posture_models():
    class Outdated(BaseModel):
        package_name: str
        severity: str

    class Posture(BaseModel):
        project: str
        period: str
        severity_counts: dict[str, int]
        outdated_dependencies: list[Outdated]
        outdated_dependencies_count: int
        gate_coverage_pct: float
        gate_passed: bool

    snapshot = Posture(
        project="example/api",
        period="2026-W10",
        severity_counts={"CRITICAL": 2, "high": 1, "moderate": 3},
        outdated_dependencies=[Outdated(package_name="lib", severity="high")],
        outdated_dependencies_count=3,
        gate_coverage_pct=87.5,
        gate_passed=False,
    )
    rows = security_rows_from_snapshots([snapshot], team_of={"example/api": "platform"})
    assert [r["severity"] for r in rows] == list(security_posture.SEVERITIES)
    by_severity = {r["severity"]: r for r in rows}
    assert by_severity["critical"]["open_findings"] == 2
    assert by_severity["medium"]["open_findings"] == 3
    assert by_severity["high"]["outdated_dependencies"] == 1
    assert by_severity["info"]["outdated_dependencies"] == 2
    assert {r["gate_coverage"] for r in rows} == {0.875}
    assert {r["gate_passed"] for r in rows} == {0}
    assert {r["snapshot_date"] for r in rows} == {"2026-03-02"}
    assert {r["team"] for r in rows} == {"platform"}
    with pytest.raises(ValueError, match="no period"):
        security_rows_from_snapshots([{"project": "x"}])


def test_synthetic_security_snapshots_have_posture_shape():
    keys = {
        "project",
        "period",
        "severity_counts",
        "outdated_dependencies",
        "outdated_dependencies_count",
        "gate_coverage_pct",
        "gate_passed",
    }
    for snapshot in generate_security_snapshots():
        assert set(snapshot) == keys


# Support metrics


def test_support_rows_from_tickets():
    created = "2026-02-10T09:00:00Z"
    answered = {
        "id": "T1",
        "source": "zendesk",
        "status": "solved",
        "product": "billing-app",
        "created_at": created,
        "solved_at": "2026-02-10T13:00:00Z",
        "csat": 1.4,
        "comments": [
            {"author_role": "customer", "created_at": created},
            {"author_role": "agent", "created_at": "2026-02-10T09:45:00Z", "public": False},
            {"author_role": "agent", "created_at": "2026-02-10T09:30:00Z"},
        ],
        "extra": {"channel": "chat", "sentiment_score": 0.6},
    }
    bot = {
        "id": "T2",
        "source": "zendesk",
        "status": "closed",
        "created_at": created,
        "solved_at": "2026-02-10T09:05:00Z",
        "comments": [],
        "extra": {"sentiment": "negative", "team": "tier2"},
    }
    waiting = {"id": "T3", "status": "open", "created_at": created, "tags": ["deflected"], "extra": {}}
    first, second, third = support_rows_from_tickets([answered, bot, waiting], team_of={"billing-app": "tier1"})
    assert first["first_response_minutes"] == 30.0
    assert first["resolution_hours"] == 4.0
    assert first["resolved"] == "yes" and first["deflected"] == "no"
    assert first["sentiment"] == "positive" and first["csat"] == 1.0
    assert first["team"] == "tier1" and first["channel"] == "chat" and first["opened_month"] == "2026-02"
    assert second["deflected"] == "yes" and second["deflected_flag"] == 1
    assert second["sentiment"] == "negative" and second["sentiment_score"] is None and second["team"] == "tier2"
    assert third["deflected"] == "no" and third["resolved_flag"] == 0 and third["sentiment"] == "unknown"


def test_support_kpis_match_rows(sqlite_executor):
    rows = [r for r in generate_model_rows()["support_metrics"] if "2026-04-01" <= r["opened_date"] <= "2026-06-30"]
    run = get_model("support_metrics").run(sqlite_executor, Q2, now=NOW)
    assert run.panel("volume").value == len(rows)
    assert run.panel("resolution_rate").value == pytest.approx(sum(r["resolved_flag"] for r in rows) / len(rows))
    assert run.panel("deflection_rate").value == pytest.approx(sum(r["deflected_flag"] for r in rows) / len(rows))
    csats = [r["csat"] for r in rows if r["csat"] is not None]
    assert run.panel("csat").value == pytest.approx(sum(csats) / len(csats))
    assert [r["sentiment"] for r in run.panel("sentiment").rows] == ["negative", "neutral", "positive"]


def test_support_project_filter(sqlite_executor):
    model = get_model("support_metrics")
    run = model.run(sqlite_executor, Q2.model_copy(update={"project": "api"}), now=NOW)
    rows = generate_model_rows()["support_metrics"]
    expected = [r for r in rows if r["project"] == "api" and "2026-04-01" <= r["opened_date"] <= "2026-06-30"]
    assert run.panel("volume").value == len(expected)


# Safety


def test_parameter_values_are_bound_and_escaped(sqlite_executor):
    model = get_model("dev_productivity")
    hostile = "x' OR '1'='1"
    plan = model.bind(model.panels[0], ModelParameters(team=hostile))
    compiled = sqlite_executor.compiler.compile(plan, model.cube)
    assert hostile not in compiled.sql
    assert hostile in compiled.params.values()
    run = model.run(sqlite_executor, ModelParameters(team=hostile), now=NOW)
    assert run.panel("prs_merged").value == 0
    html = render_dashboard(model.run(sqlite_executor, ModelParameters(team="<script>alert(1)</script>"), now=NOW))
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_panel_error_is_rendered_not_raised():
    with SQLiteExecutor(":memory:") as empty:
        run = get_model("support_metrics").run(empty, Q2, now=NOW)
    assert set(run.errors) == {p.id for p in get_model("support_metrics").panels}
    html = render_dashboard(run)
    assert "Could not load" in html


# Formatting and headlines


def test_format_number_units_and_locales():
    assert format_number(1234, "count") == "1,234"
    assert format_number(1234, "count", "pt-BR") == "1.234"
    assert format_number(12.345, "hours") == "12.3 h"
    assert format_number(12.345, "hours", "pt-BR") == "12,3 h"
    assert format_number(0.875, "percent") == "87.5%"
    assert format_number(0.875, "percent", "pt-BR") == "87,5%"
    assert format_number(35.6, "minutes") == "36 min"
    assert format_number(None) == "n/a"
    assert format_number("abc") == "n/a"


def test_compare_text_wording():
    assert compare_text(120, 100) == "up 20% vs. previous period (better)"
    assert compare_text(80, 100, "hours", better="lower") == "down 20% vs. previous period (better)"
    assert compare_text(0.9, 0.8, "percent") == "up 10.0 pp vs. previous period (better)"
    assert compare_text(90, 100, better="none") == "down 10% vs. previous period"
    assert compare_text(100, 100) == "no change vs. previous period"
    assert compare_text(5, 0) == "previous period: 0"
    assert compare_text(5, None) == ""


def _result(kind, labels, values, **panel_kwargs):
    panel = Panel(
        id="p",
        kind=kind,
        title="Test",
        plan=QueryPlan(cube="dev_productivity", metrics=["item_count"], dimensions=["person"]),
        x="person",
        y="item_count",
        **panel_kwargs,
    )
    chart = ComputedChart(id="p", type=kind, title="Test", x="person", y="item_count", labels=labels, values=values)
    return PanelResult(panel=panel, columns=["person", "item_count"], rows=[{}] * len(labels), chart=chart)


def test_headlines_state_the_finding():
    line = _result("line", ["2026-01", "2026-02", "2026-03"], [10, 30, 15])
    assert headline(line) == "Up 50% from 2026-01 to 2026-03, peak of 30 in 2026-02"
    flat = _result("line", ["a", "b"], [10, 10])
    assert headline(flat) == "Flat from a to b"
    bar = _result("bar", ["ana", "bo", "cy"], [3, 9, 5])
    assert headline(bar) == "bo leads with 9"
    highlighted = _result("bar", ["critical", "high"], [2, 6], keep_order=True, highlight="critical")
    assert headline(highlighted) == "critical: 2 of 8 (25%)"


def test_bars_sorted_by_value_from_zero():
    from nexdash.report_models.render import bar_svg

    svg = bar_svg(_result("bar", ["ana", "bo", "cy"], [3, 9, 5]), "t")
    assert re.findall(r'fill="#1A1A1A">([a-z]+)</text>', svg) == ["bo", "cy", "ana"]
    xs = set(re.findall(r'<rect x="(\d+)"', svg))
    assert xs == {"160"}
    assert svg.count('fill="#1F5FAD"') == 1
    assert 'role="img"' in svg and "<desc" in svg


# Integration with reports, dashboards and interfaces


def test_report_specs_preview_through_report_store(sqlite_executor):
    model = get_model("dev_productivity")
    store = ReportStore(registry=model.registry(), executor=sqlite_executor)
    try:
        spec = model.report_specs()["dev_productivity_throughput_team"]
        preview = store.preview("dev_productivity_throughput_team", spec=spec, params={"team": "data"})
    finally:
        store.close()
    assert [row["team"] for row in preview.rows] == ["data"]
    assert preview.charts[0].type == "bar"


def test_publish_dashboard_snapshot(sqlite_executor):
    run = get_model("support_metrics").run(sqlite_executor, Q2, now=NOW)
    store = DashboardStore()
    published = publish_dashboard(store, run, owner_email="owner@example.com")
    rendered = store.render_by_access_token(published["access_token"])
    assert rendered is not None
    assert "Support metrics" in rendered.html
    assert "connect-src 'none'" in rendered.headers["Content-Security-Policy"]


def test_service_registers_model_cubes_and_queries_them():
    with DatabaseExecutor("sqlite:///:memory:") as ex:
        service = build_service(ex, demo=True, report_models=True)
        names = [cube["name"] for cube in service.list_cubes()]
        assert set(MODEL_NAMES) <= set(names)
        assert {"sales", "stock", "support"} <= set(names)
        result = service.run_plan(
            {"cube": "support_metrics", "dimensions": ["channel"], "metrics": ["ticket_count"]}
        )
        assert len(result["rows"]) == 4


def test_register_model_cubes_rejects_duplicates():
    registry = get_model("dev_productivity").registry()
    with pytest.raises(ValueError, match="already registered"):
        register_model_cubes(registry)
