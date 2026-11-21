"""Semantic cube definitions and YAML loader.

A cube defines an analytical entity: its underlying source (table or view),
dimensions, metrics with aggregations, allowed joins, and allowed filters.
Free-form SQL is prohibited; all access must follow the cube allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

AggregationType = Literal["count", "count_distinct", "sum", "avg", "min", "max"]
DimensionType = Literal["string", "number", "integer", "float", "date", "datetime", "boolean"]
FilterOperator = Literal[
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "like",
    "not_like",
    "between",
    "is_null",
    "is_not_null",
]
JoinRelationship = Literal["many_to_one", "one_to_many", "one_to_one"]

IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
QUALIFIED_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


def validate_identifier(name: str, label: str = "identifier") -> str:
    """Validate that a string is a safe SQL identifier."""
    if not isinstance(name, str) or not IDENTIFIER_PATTERN.match(name):
        raise ValueError(f"Invalid {label} '{name}': must match ^[a-zA-Z_][a-zA-Z0-9_]*$")
    return name


def validate_qualified_identifier(name: str, label: str = "identifier") -> str:
    """Validate that a string is a safe qualified SQL identifier (e.g. table.column)."""
    if not isinstance(name, str) or not QUALIFIED_IDENTIFIER_PATTERN.match(name):
        raise ValueError(f"Invalid {label} '{name}': must match qualified identifier pattern")
    return name


class Dimension(BaseModel):
    """A dimension represents an attribute or grouping field in a cube."""

    name: str
    column: str | None = None
    type: DimensionType = "string"
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_identifier(v, "dimension name")

    @field_validator("column")
    @classmethod
    def _validate_column(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_qualified_identifier(v, "dimension column")
        return v

    @model_validator(mode="after")
    def _set_default_column(self) -> Dimension:
        if self.column is None:
            self.column = self.name
        return self


class Metric(BaseModel):
    """A metric represents a quantitative measurement computed via aggregation."""

    name: str
    aggregation: AggregationType
    column: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_identifier(v, "metric name")

    @field_validator("column")
    @classmethod
    def _validate_column(cls, v: str | None) -> str | None:
        if v is not None and v != "*":
            return validate_qualified_identifier(v, "metric column")
        return v

    @model_validator(mode="after")
    def _validate_aggregation_and_column(self) -> Metric:
        if self.aggregation != "count" and not self.column:
            raise ValueError(f"Metric '{self.name}' with aggregation '{self.aggregation}' requires a column")
        return self


class Join(BaseModel):
    """An allowed join relationship between the cube and another entity."""

    name: str
    target_table: str | None = None
    relationship: JoinRelationship = "many_to_one"
    source_key: str
    target_key: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_identifier(v, "join name")

    @field_validator("target_table")
    @classmethod
    def _validate_target_table(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_identifier(v, "join target_table")
        return v

    @field_validator("source_key")
    @classmethod
    def _validate_source_key(cls, v: str) -> str:
        return validate_identifier(v, "join source_key")

    @field_validator("target_key")
    @classmethod
    def _validate_target_key(cls, v: str) -> str:
        return validate_identifier(v, "join target_key")

    @model_validator(mode="after")
    def _set_default_target_table(self) -> Join:
        if self.target_table is None:
            self.target_table = self.name
        return self


class FilterRule(BaseModel):
    """An allowed filter configuration for a field."""

    name: str
    operators: list[FilterOperator] | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_identifier(v, "filter name")


class Cube(BaseModel):
    """A semantic cube declaring dimensions, metrics, joins, and allowed filters."""

    name: str
    source: str
    dimensions: list[Dimension] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    filters: list[FilterRule] = Field(default_factory=list)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_identifier(v, "cube name")

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        return validate_identifier(v, "cube source")

    @field_validator("filters", mode="before")
    @classmethod
    def _normalize_filters(cls, v: Any) -> list[Any]:
        if not v:
            return []
        normalized = []
        for item in v:
            if isinstance(item, str):
                normalized.append({"name": item})
            else:
                normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def _validate_uniqueness_and_consistency(self) -> Cube:
        # Check dimension uniqueness
        dim_names: set[str] = set()
        for dim in self.dimensions:
            if dim.name in dim_names:
                raise ValueError(f"Duplicate dimension name '{dim.name}' in cube '{self.name}'")
            dim_names.add(dim.name)

        # Check metric uniqueness and collision with dimensions
        metric_names: set[str] = set()
        for metric in self.metrics:
            if metric.name in metric_names:
                raise ValueError(f"Duplicate metric name '{metric.name}' in cube '{self.name}'")
            if metric.name in dim_names:
                raise ValueError(
                    f"Metric name '{metric.name}' conflicts with dimension name in cube '{self.name}'"
                )
            metric_names.add(metric.name)

        # Check join uniqueness
        join_names: set[str] = set()
        for join in self.joins:
            if join.name in join_names:
                raise ValueError(f"Duplicate join name '{join.name}' in cube '{self.name}'")
            join_names.add(join.name)

        return self

    def get_dimension(self, name: str) -> Dimension | None:
        """Find a dimension by name."""
        for dim in self.dimensions:
            if dim.name == name:
                return dim
        return None

    def get_metric(self, name: str) -> Metric | None:
        """Find a metric by name."""
        for metric in self.metrics:
            if metric.name == name:
                return metric
        return None

    def get_join(self, name: str) -> Join | None:
        """Find a join by name."""
        for join in self.joins:
            if join.name == name:
                return join
        return None

    @property
    def allowed_dimension_names(self) -> set[str]:
        return {dim.name for dim in self.dimensions}

    @property
    def allowed_metric_names(self) -> set[str]:
        return {metric.name for metric in self.metrics}

    @property
    def allowed_join_names(self) -> set[str]:
        return {join.name for join in self.joins}

    @property
    def allowed_filter_names(self) -> set[str]:
        """Names of fields allowed in filters.

        If explicit filters are declared, returns that set.
        Otherwise, defaults to allowing all declared dimensions.
        """
        if self.filters:
            return {f.name for f in self.filters}
        return self.allowed_dimension_names

    def is_filter_allowed(self, field: str, operator: str) -> bool:
        """Check whether a filter on a given field and operator is allowed."""
        if self.filters:
            for rule in self.filters:
                if rule.name == field:
                    if rule.operators is None:
                        return True
                    return operator in rule.operators
            return False
        return field in self.allowed_dimension_names


def load_cube_from_yaml(path_or_content: str | Path) -> Cube:
    """Load and validate a Cube from a YAML file path or YAML string content."""
    raw: Any
    is_path = False
    if isinstance(path_or_content, Path):
        is_path = True
    elif isinstance(path_or_content, str) and "\n" not in path_or_content and "\r" not in path_or_content:
        is_path = Path(path_or_content).exists()

    if is_path:
        path = Path(path_or_content)
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    else:
        raw = yaml.safe_load(str(path_or_content))

    if not isinstance(raw, dict):
        raise ValueError("Cube YAML content must be a mapping/dictionary")

    return Cube.model_validate(raw)


def load_cubes(directory: str | Path) -> dict[str, Cube]:
    """Load all cube YAML files (*.yaml, *.yml) from a directory."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise FileNotFoundError(f"Directory not found: {directory}")

    cubes: dict[str, Cube] = {}
    patterns = ("*.yaml", "*.yml")
    for pattern in patterns:
        for file_path in sorted(dir_path.glob(pattern)):
            cube = load_cube_from_yaml(file_path)
            if cube.name in cubes:
                raise ValueError(f"Duplicate cube '{cube.name}' loaded from {file_path.name}")
            cubes[cube.name] = cube

    return cubes


class CubeRegistry:
    """Registry for managing and looking up available cubes."""

    def __init__(self, cubes: dict[str, Cube] | None = None) -> None:
        self._cubes: dict[str, Cube] = dict(cubes or {})

    def register(self, cube: Cube) -> None:
        """Register a cube."""
        self._cubes[cube.name] = cube

    def get(self, name: str) -> Cube:
        """Retrieve a cube by name or raise KeyError."""
        if name not in self._cubes:
            raise KeyError(f"Cube '{name}' not found in registry")
        return self._cubes[name]

    def __contains__(self, name: str) -> bool:
        return name in self._cubes

    def list_cubes(self) -> list[str]:
        """List registered cube names."""
        return sorted(self._cubes.keys())

    @classmethod
    def from_directory(cls, directory: str | Path) -> CubeRegistry:
        """Create a registry by loading all cubes in a directory."""
        return cls(load_cubes(directory))
