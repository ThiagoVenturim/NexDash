"""Structured query plan models.

The query plan represents an analytical request: which cube to query,
metrics to compute, dimensions to group by, filters to apply, time range,
sorting, and limits. Free-form SQL is strictly forbidden; all fields are
typed and validated through Pydantic.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from askdash.cube import (
    FilterOperator,
    validate_identifier,
    validate_qualified_identifier,
)

SortDirection = Literal["asc", "desc"]
ScalarValue = str | int | float | bool | date | datetime


class PlanFilter(BaseModel):
    """A filter condition on a specific dimension or attribute."""

    field: str
    operator: FilterOperator = "eq"
    value: Any = None

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        return validate_qualified_identifier(v, "filter field")

    @model_validator(mode="after")
    def _validate_operator_value(self) -> PlanFilter:
        op = self.operator
        val = self.value

        if op in ("is_null", "is_not_null"):
            if val is not None:
                self.value = None
            return self

        if op in ("in", "not_in"):
            if not isinstance(val, (list, tuple, set)):
                raise ValueError(f"Operator '{op}' requires a list/tuple of values, got {type(val).__name__}")
            if len(val) == 0:
                raise ValueError(f"Operator '{op}' requires at least one value")
            # Ensure elements are scalar primitives
            for item in val:
                if not isinstance(item, (str, int, float, bool, date, datetime)):
                    raise ValueError(f"Operator '{op}' values must be scalars, got {type(item).__name__}")
            return self

        if op == "between":
            if not isinstance(val, (list, tuple)) or len(val) != 2:
                raise ValueError("Operator 'between' requires a 2-element sequence [start, end]")
            for item in val:
                if not isinstance(item, (str, int, float, bool, date, datetime)):
                    raise ValueError(f"Operator 'between' values must be scalars, got {type(item).__name__}")
            return self

        if val is None:
            raise ValueError(f"Operator '{op}' requires a non-null value")

        if not isinstance(val, (str, int, float, bool, date, datetime)):
            raise ValueError(f"Filter value must be a scalar primitive, got {type(val).__name__}")

        return self


class TimeRange(BaseModel):
    """A time range filter for date/datetime dimensions."""

    field: str
    start: date | datetime | str | None = None
    end: date | datetime | str | None = None
    preset: str | None = None

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        return validate_qualified_identifier(v, "time_range field")

    @field_validator("preset")
    @classmethod
    def _validate_preset(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_identifier(v, "time_range preset")
        return v

    @model_validator(mode="after")
    def _validate_bounds(self) -> TimeRange:
        if self.start is None and self.end is None and self.preset is None:
            raise ValueError("TimeRange requires at least one of start, end, or preset")
        return self


class PlanOrderBy(BaseModel):
    """Sort specification for query results."""

    field: str
    direction: SortDirection = "asc"

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        return validate_qualified_identifier(v, "order_by field")


class QueryPlan(BaseModel):
    """A strictly validated analytical query plan.

    The plan declares the target cube, requested metrics and dimensions,
    filters, time range, ordering, and limits. No free SQL is accepted.
    """

    cube: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[PlanFilter] = Field(default_factory=list)
    time_range: TimeRange | None = None
    order_by: list[PlanOrderBy] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=100000)
    offset: int | None = Field(default=None, ge=0)
    joins: list[str] = Field(default_factory=list)

    @field_validator("cube")
    @classmethod
    def _validate_cube(cls, v: str) -> str:
        return validate_identifier(v, "query plan cube")

    @field_validator("metrics", "dimensions", "joins")
    @classmethod
    def _validate_identifiers_list(cls, items: list[str]) -> list[str]:
        validated = []
        for item in items:
            validated.append(validate_qualified_identifier(item, "field"))
        return validated

    @model_validator(mode="after")
    def _validate_plan_content(self) -> QueryPlan:
        if not self.metrics and not self.dimensions:
            raise ValueError("QueryPlan must request at least one metric or dimension")

        # Check for duplicates in metrics
        metric_set: set[str] = set()
        for m in self.metrics:
            if m in metric_set:
                raise ValueError(f"Duplicate metric '{m}' in QueryPlan")
            metric_set.add(m)

        # Check for duplicates in dimensions
        dim_set: set[str] = set()
        for d in self.dimensions:
            if d in dim_set:
                raise ValueError(f"Duplicate dimension '{d}' in QueryPlan")
            dim_set.add(d)

        return self
