"""Ready-made report models: development productivity, security posture and support metrics.

Each model is one cube on the semantic layer plus a dashboard template. Panels are
``QueryPlan`` objects compiled by the guarded SQL compiler; period, team and project
parameters are bound as SQL parameters. Typical use::

    from nexdash import SQLiteExecutor
    from nexdash.report_models import ModelParameters, get_model, render_dashboard
    from nexdash.report_models.synthetic import seed_model_data

    with SQLiteExecutor(":memory:") as executor:
        seed_model_data(executor)
        model = get_model("dev_productivity")
        run = model.run(executor, ModelParameters(start="2026-04-01", end="2026-06-30", team="apps"))
        html = render_dashboard(run)
"""

from __future__ import annotations

from nexdash.cube import CubeRegistry
from nexdash.report_models import dev_productivity, security_posture, support_metrics
from nexdash.report_models.base import (
    MAX_KPIS,
    ModelParameters,
    ModelRun,
    Panel,
    PanelResult,
    ReportModel,
    ReportModelError,
)
from nexdash.report_models.dev_productivity import dev_rows_from_work_items
from nexdash.report_models.render import (
    compare_text,
    format_number,
    headline,
    publish_dashboard,
    render_dashboard,
)
from nexdash.report_models.security_posture import period_start, security_rows_from_snapshots
from nexdash.report_models.support_metrics import support_rows_from_tickets

REPORT_MODELS: dict[str, ReportModel] = {
    model.name: model for model in (dev_productivity.MODEL, security_posture.MODEL, support_metrics.MODEL)
}


def get_model(name: str) -> ReportModel:
    """Return a report model by name."""
    try:
        return REPORT_MODELS[name]
    except KeyError:
        raise KeyError(f"Unknown report model '{name}'. Available: {', '.join(sorted(REPORT_MODELS))}") from None


def register_model_cubes(registry: CubeRegistry) -> list[str]:
    """Add the cube of every report model to ``registry`` (for MCP, REST and reports). Returns the names."""
    names = []
    for model in REPORT_MODELS.values():
        if model.cube.name in registry:
            raise ValueError(f"Cube '{model.cube.name}' is already registered")
        registry.register(model.cube)
        names.append(model.cube.name)
    return names


__all__ = [
    "MAX_KPIS",
    "REPORT_MODELS",
    "ModelParameters",
    "ModelRun",
    "Panel",
    "PanelResult",
    "ReportModel",
    "ReportModelError",
    "compare_text",
    "dev_rows_from_work_items",
    "format_number",
    "get_model",
    "headline",
    "period_start",
    "publish_dashboard",
    "register_model_cubes",
    "render_dashboard",
    "security_rows_from_snapshots",
    "support_rows_from_tickets",
]
