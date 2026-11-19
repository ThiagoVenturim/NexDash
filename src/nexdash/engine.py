"""Shared report engine: report models served to other projects over MCP and REST.

Other projects (for example a coding agent or a support engine) ask nexdash for a
report instead of building their own. The engine exposes three operations:

- ``list_models``: the report models with their parameters and panels.
- ``describe_model``: one model in detail, with the JSON schema of its parameters.
- ``run``: run a model with validated parameters and return the panel data, a
  self-contained HTML dashboard, or both.

Callers never send SQL or query plans here. A request names a model and passes
``start``, ``end``, ``team`` and ``project``; the panels of the model are fixed
``QueryPlan`` objects and the parameters are bound as SQL parameters by the
guarded compiler. Unknown fields, unknown models and malformed values are refused
with a client-safe message. Database errors are logged by type only.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexdash.executor import DatabaseExecutor, SQLiteExecutor
from nexdash.report_models import REPORT_MODELS, ModelParameters, ModelRun, ReportModel, render_dashboard
from nexdash.service import NotFoundError, ServiceError

logger = logging.getLogger("nexdash.engine")

ENGINE_VERSION = "1"
DEFAULT_MAX_PANEL_ROWS = 500
OUTPUTS = ("data", "dashboard", "both")
LOCALES = ("en", "pt-BR")
DASHBOARD_CONTENT_TYPE = "text/html; charset=utf-8"

# Report dashboards are plain HTML with one <style> block and inline SVG: no scripts,
# no network, no external assets.
REPORT_DASHBOARD_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

PANEL_ERROR_MESSAGE = "The panel query failed on the server."


class EngineError(ServiceError):
    """A report engine error with a client-safe message (status 400 unless set)."""


class RunRequest(BaseModel):
    """What a client sends to generate a report. Extra fields are refused."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    output: Literal["data", "dashboard", "both"] = "both"
    locale: Literal["en", "pt-BR"] = "en"
    compare: bool = True


def _validation_message(prefix: str, exc: ValidationError) -> str:
    # Messages only: pydantic's str() would echo the rejected input back.
    details = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'request'}: {err['msg']}" for err in exc.errors())
    return f"{prefix}: {details}"


class ReportEngine:
    """Runs report models for remote clients. Transport-neutral: MCP and REST both call it."""

    def __init__(
        self,
        executor: DatabaseExecutor | SQLiteExecutor,
        models: Mapping[str, ReportModel] | None = None,
        *,
        max_panel_rows: int = DEFAULT_MAX_PANEL_ROWS,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.executor = executor
        self.models: dict[str, ReportModel] = dict(models if models is not None else REPORT_MODELS)
        self.max_panel_rows = max_panel_rows
        self._clock = clock

    # -- discovery ---------------------------------------------------------

    def list_models(self) -> list[dict[str, Any]]:
        return [self._summary(self.models[name]) for name in sorted(self.models)]

    def describe_model(self, name: Any) -> dict[str, Any]:
        model = self._get(name)
        info = self._summary(model)
        info["source_note"] = model.source_note
        info["parameters_schema"] = ModelParameters.model_json_schema()
        info["request_schema"] = RunRequest.model_json_schema()
        info["panels"] = [
            {
                "id": panel.id,
                "kind": panel.kind,
                "title": panel.title,
                "unit": panel.unit,
                "x": panel.x,
                "y": panel.y,
                "description": panel.description,
            }
            for panel in model.panels
        ]
        return info

    # -- generation --------------------------------------------------------

    def parse_request(self, request: RunRequest | Mapping[str, Any] | Any) -> RunRequest:
        if isinstance(request, RunRequest):
            return request
        if not isinstance(request, Mapping):
            raise EngineError("The report request must be a JSON object.")
        try:
            parsed = RunRequest.model_validate(dict(request))
        except ValidationError as exc:
            raise EngineError(_validation_message("Invalid report request", exc)) from None
        self._get(parsed.model)
        return parsed

    def execute(self, request: RunRequest | Mapping[str, Any] | Any) -> tuple[RunRequest, ModelRun]:
        """Validate the request and run every panel of the model."""
        parsed = self.parse_request(request)
        model = self._get(parsed.model)
        now = self._clock() if self._clock else None
        try:
            run = model.run(self.executor, parsed.parameters, compare=parsed.compare, now=now)
        except Exception as exc:
            logger.error("Report model '%s' failed: %s", model.name, type(exc).__name__)
            raise EngineError("The report failed while running on the database.", status=500) from None
        for panel_id, error in run.errors.items():
            # The detail may hold SQL or driver text; it stays in the server log as a type name only.
            logger.warning("Panel '%s' of model '%s' failed: %s", panel_id, model.name, error.split(":", 1)[0])
        return parsed, run

    def run(self, request: RunRequest | Mapping[str, Any] | Any) -> dict[str, Any]:
        """Run a model and return a JSON-friendly payload with data and/or the rendered dashboard."""
        parsed, run = self.execute(request)
        payload: dict[str, Any] = {
            "engine_version": ENGINE_VERSION,
            "model": run.model.name,
            "title": run.model.title,
            "parameters": parsed.parameters.model_dump(mode="json"),
            "filters": run.params.describe(),
            "generated_at": run.generated_at,
            "errors": {panel_id: PANEL_ERROR_MESSAGE for panel_id in run.errors},
        }
        if parsed.output in ("data", "both"):
            payload["data"] = {"panels": self._dataset(run)}
        if parsed.output in ("dashboard", "both"):
            payload["dashboard"] = {
                "format": "html",
                "content_type": DASHBOARD_CONTENT_TYPE,
                "csp": REPORT_DASHBOARD_CSP,
                "locale": parsed.locale,
                "html": render_dashboard(run, locale=parsed.locale),
            }
        return payload

    def render(self, request: RunRequest | Mapping[str, Any] | Any) -> str:
        """Run a model and return only the HTML dashboard."""
        parsed, run = self.execute(request)
        return render_dashboard(run, locale=parsed.locale)

    # -- helpers -----------------------------------------------------------

    def _get(self, name: Any) -> ReportModel:
        if not isinstance(name, str) or name not in self.models:
            available = ", ".join(sorted(self.models))
            raise NotFoundError(f"Report model '{name}' not found. Available: {available}.")
        return self.models[name]

    def _summary(self, model: ReportModel) -> dict[str, Any]:
        return {
            "name": model.name,
            "title": model.title,
            "description": model.description,
            "cube": model.cube.name,
            "parameters": ["start", "end", "team", "project"],
            "panel_ids": [panel.id for panel in model.panels],
            "outputs": list(OUTPUTS),
            "locales": list(LOCALES),
        }

    def _dataset(self, run: ModelRun) -> dict[str, Any]:
        dataset = run.dataset()
        for entry in dataset.values():
            if entry.get("error"):
                entry["error"] = PANEL_ERROR_MESSAGE
            rows = entry.get("rows") or []
            entry["truncated"] = len(rows) > self.max_panel_rows
            entry["rows"] = rows[: self.max_panel_rows]
        return dataset


__all__ = [
    "DASHBOARD_CONTENT_TYPE",
    "ENGINE_VERSION",
    "LOCALES",
    "OUTPUTS",
    "REPORT_DASHBOARD_CSP",
    "EngineError",
    "ReportEngine",
    "RunRequest",
]
