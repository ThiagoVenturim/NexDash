"""nexdash: dashboards and reports from natural-language questions.

Questions are answered through a semantic layer of YAML cubes: the model picks
cubes and fills a structured plan, and nexdash compiles that plan into
parameterized SQL. The model never writes free SQL.
"""

from nexdash.compiler import AllowlistError, CompilationError, CompiledQuery, SQLCompiler
from nexdash.cube import (
    AggregationType,
    Cube,
    CubeRegistry,
    Dimension,
    DimensionType,
    FilterOperator,
    FilterRule,
    Join,
    JoinRelationship,
    Metric,
    load_cube_from_yaml,
    load_cubes,
)
from nexdash.executor import ExecutionResult, SQLiteExecutor
from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan, TimeRange

__version__ = "0.1.0"

__all__ = [
    "AggregationType",
    "AllowlistError",
    "CompilationError",
    "CompiledQuery",
    "Cube",
    "CubeRegistry",
    "Dimension",
    "DimensionType",
    "ExecutionResult",
    "FilterOperator",
    "FilterRule",
    "Join",
    "JoinRelationship",
    "Metric",
    "PlanFilter",
    "PlanOrderBy",
    "QueryPlan",
    "SQLCompiler",
    "SQLiteExecutor",
    "TimeRange",
    "__version__",
    "load_cube_from_yaml",
    "load_cubes",
]
