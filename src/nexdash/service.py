"""Transport-neutral query service shared by the MCP server, the REST API and the web UI.

Every entry point goes through the same path: a ``QueryPlan`` (written by a
client or produced by the model from a question) is validated, compiled through
the cube allowlist into parameterized SQL, and executed. Errors are reported
with messages that are safe to return to a client; database exceptions are
logged by type only, so connection strings or values never reach logs or
responses.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from nexdash.compiler import CompilationError
from nexdash.cube import CubeRegistry
from nexdash.executor import DatabaseExecutor, SQLiteExecutor
from nexdash.plan import QueryPlan
from nexdash.planner import PlanGenerator, PlannerError, describe_cube

logger = logging.getLogger("nexdash.service")

DEFAULT_MAX_ROWS = 1000


class ServiceError(Exception):
    """An error with a client-safe message and an HTTP-like status code."""

    status = 400

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status


class NotFoundError(ServiceError):
    status = 404


class QueryService:
    """Lists cubes, describes them, runs plans and answers questions."""

    def __init__(
        self,
        registry: CubeRegistry,
        executor: DatabaseExecutor | SQLiteExecutor,
        *,
        planner: PlanGenerator | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self.registry = registry
        self.executor = executor
        self.planner = planner
        self.max_rows = max_rows

    def list_cubes(self) -> list[dict[str, Any]]:
        cubes = []
        for name in self.registry.list_cubes():
            cube = self.registry.get(name)
            cubes.append(
                {
                    "name": cube.name,
                    "description": cube.description or "",
                    "dimensions": [dim.name for dim in cube.dimensions],
                    "metrics": [metric.name for metric in cube.metrics],
                }
            )
        return cubes

    def describe_cube(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self.registry:
            raise NotFoundError(f"Cube '{name}' not found.")
        return describe_cube(self.registry.get(name))

    def parse_plan(self, plan: QueryPlan | dict[str, Any] | Any) -> QueryPlan:
        if isinstance(plan, QueryPlan):
            return plan
        if not isinstance(plan, dict):
            raise ServiceError("The plan must be a JSON object.")
        try:
            return QueryPlan.model_validate(plan)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'plan'}: {err['msg']}" for err in exc.errors()
            )
            raise ServiceError(f"Invalid plan: {details}") from None

    def run_plan(self, plan: QueryPlan | dict[str, Any] | Any) -> dict[str, Any]:
        parsed = self.parse_plan(plan)
        if parsed.cube not in self.registry:
            raise NotFoundError(f"Cube '{parsed.cube}' not found.")
        if parsed.limit is None or parsed.limit > self.max_rows:
            parsed = parsed.model_copy(update={"limit": self.max_rows})
        cube = self.registry.get(parsed.cube)

        try:
            compiled = self.executor.compiler.compile(
                parsed, cube, registry=self.registry, dialect=self._dialect()
            )
        except CompilationError as exc:
            raise ServiceError(str(exc)) from None

        try:
            result = self.executor.execute_compiled(compiled)
        except Exception as exc:
            logger.error("Query on cube '%s' failed: %s", parsed.cube, type(exc).__name__)
            raise ServiceError("The query failed while running on the database.", status=500) from None

        return {
            "cube": parsed.cube,
            "plan": parsed.model_dump(mode="json", exclude_none=True),
            "sql": compiled.sql,
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
        }

    def ask(self, question: Any) -> dict[str, Any]:
        if self.planner is None:
            raise ServiceError("Natural-language questions are disabled on this server.", status=501)
        if not isinstance(question, str):
            raise ServiceError("The question must be a string.")
        try:
            plan = self.planner.plan(question, self.registry)
        except PlannerError as exc:
            raise ServiceError(str(exc), status=422) from None
        except Exception as exc:
            logger.error("Model call failed: %s", type(exc).__name__)
            raise ServiceError("The language model call failed.", status=502) from None
        result = self.run_plan(plan)
        result["question"] = question.strip()
        return result

    def _dialect(self) -> str:
        return getattr(self.executor, "dialect_name", "sqlite")


__all__ = ["DEFAULT_MAX_ROWS", "NotFoundError", "QueryService", "ServiceError"]
