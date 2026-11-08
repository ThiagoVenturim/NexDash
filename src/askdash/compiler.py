"""SQL Compiler for structured query plans.

Translates a QueryPlan into parameterized SQL strictly through an allowlist
defined by the target Cube. Column names, table names, joins, and filters
must be explicitly declared. User-supplied filter values are always bound
as parameters; string interpolation of values is never permitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from askdash.cube import Cube, CubeRegistry
from askdash.plan import PlanFilter, QueryPlan


class CompilationError(Exception):
    """Base exception for query plan compilation errors."""


class AllowlistError(CompilationError):
    """Raised when a query plan references a field, metric, join, or filter not in the allowlist."""


@dataclass(frozen=True)
class CompiledQuery:
    """The result of compiling a QueryPlan into parameterized SQL."""

    sql: str
    params: dict[str, Any]
    dialect: str
    statement: sa.sql.Select


def _get_dialect(name: str) -> sa.engine.interfaces.Dialect:
    """Return a SQLAlchemy dialect configured for named parameters."""
    normalized = name.lower().strip()
    if normalized in ("sqlite", "sqlite3"):
        return sqlite.dialect(paramstyle="named")
    if normalized in ("postgresql", "postgres"):
        from sqlalchemy.dialects import postgresql

        return postgresql.dialect(paramstyle="named")
    # Default fallback to sqlite dialect with named parameters
    return sqlite.dialect(paramstyle="named")


def _get_table_col(table: sa.TableClause, col_name: str) -> sa.ColumnClause[Any]:
    """Retrieve or append a column to a TableClause so it renders as table.col."""
    if col_name not in table.c:
        table.append_column(sa.column(col_name))
    return table.c[col_name]


class SQLCompiler:
    """Compiles a validated QueryPlan into parameterized SQL using Cube allowlists."""

    def __init__(self, default_dialect: str = "sqlite") -> None:
        self.default_dialect = default_dialect

    def compile(
        self,
        plan: QueryPlan,
        cube: Cube,
        registry: CubeRegistry | None = None,
        dialect: str | None = None,
    ) -> CompiledQuery:
        """Compile a QueryPlan into a CompiledQuery.

        Args:
            plan: The structured query plan to compile.
            cube: The target semantic cube.
            registry: Optional registry containing other cubes (e.g. for joins).
            dialect: Dialect name (e.g. 'sqlite', 'postgresql').

        Returns:
            A CompiledQuery containing parameterized SQL and parameter values.

        Raises:
            AllowlistError: If any metric, dimension, join, or filter is not allowed.
            CompilationError: If compilation fails.
        """
        target_dialect_name = dialect or self.default_dialect
        self._validate_allowlists(plan, cube)

        # 1. Base table & required joins
        main_table = sa.table(cube.source)
        joins_needed = set(plan.joins)

        # Scan dimensions and filters for qualified join references (e.g. "customers.city")
        for dim_name in plan.dimensions:
            if "." in dim_name:
                join_name, _ = dim_name.split(".", 1)
                joins_needed.add(join_name)

        for flt in plan.filters:
            if "." in flt.field:
                join_name, _ = flt.field.split(".", 1)
                joins_needed.add(join_name)

        from_clause: Any = main_table
        target_tables: dict[str, sa.TableClause] = {
            cube.name: main_table,
            cube.source: main_table,
        }

        for join_name in sorted(joins_needed):
            join_def = cube.get_join(join_name)
            if join_def is None:
                raise AllowlistError(
                    f"Join '{join_name}' required by plan is not declared in cube '{cube.name}'"
                )
            target_table_name = join_def.target_table or join_name
            target_tbl = sa.table(target_table_name)
            target_tables[join_name] = target_tbl
            target_tables[target_table_name] = target_tbl

            # Join condition: main_table.source_key == target_table.target_key
            source_col = _get_table_col(main_table, join_def.source_key)
            target_col = _get_table_col(target_tbl, join_def.target_key)
            from_clause = from_clause.join(target_tbl, source_col == target_col)

        # 2. Select expressions (dimensions & metrics)
        select_exprs: list[sa.sql.ColumnElement[Any]] = []
        group_by_exprs: list[sa.sql.ColumnElement[Any]] = []
        column_map: dict[str, sa.sql.ColumnElement[Any]] = {}

        # Dimensions
        for dim_name in plan.dimensions:
            col_expr = self._resolve_dimension_expr(dim_name, cube, main_table, target_tables)
            labeled_expr = col_expr.label(dim_name)
            select_exprs.append(labeled_expr)
            group_by_exprs.append(col_expr)
            column_map[dim_name] = col_expr

        # Metrics
        for metric_name in plan.metrics:
            metric_def = cube.get_metric(metric_name)
            if metric_def is None:
                raise AllowlistError(f"Metric '{metric_name}' is not declared in cube '{cube.name}'")
            metric_expr = self._build_metric_expr(metric_def, cube, main_table, target_tables)
            labeled_expr = metric_expr.label(metric_name)
            select_exprs.append(labeled_expr)
            column_map[metric_name] = metric_expr

        # Start building the statement
        stmt = sa.select(*select_exprs).select_from(from_clause)

        # 3. Where and Having clauses with bound parameters
        where_conditions: list[sa.sql.BinaryExpression[Any] | sa.sql.ColumnElement[Any]] = []
        having_conditions: list[sa.sql.BinaryExpression[Any] | sa.sql.ColumnElement[Any]] = []

        param_counter = 0

        for flt in plan.filters:
            param_counter += 1
            if flt.field in cube.allowed_metric_names:
                # Metric filter belongs in HAVING
                metric_def = cube.get_metric(flt.field)
                assert metric_def is not None
                metric_expr = self._build_metric_expr(metric_def, cube, main_table, target_tables)
                cond = self._build_filter_condition(metric_expr, flt, param_counter)
                having_conditions.append(cond)
            else:
                # Dimension filter belongs in WHERE
                dim_expr = self._resolve_dimension_expr(flt.field, cube, main_table, target_tables)
                cond = self._build_filter_condition(dim_expr, flt, param_counter)
                where_conditions.append(cond)

        # 4. Time range filter
        if plan.time_range:
            param_counter += 1
            tr = plan.time_range
            tr_expr = self._resolve_dimension_expr(tr.field, cube, main_table, target_tables)
            if tr.start is not None:
                param_name = f"tr_start_{param_counter}"
                where_conditions.append(tr_expr >= sa.bindparam(param_name, tr.start))
            if tr.end is not None:
                param_name = f"tr_end_{param_counter}"
                where_conditions.append(tr_expr <= sa.bindparam(param_name, tr.end))

        if where_conditions:
            stmt = stmt.where(sa.and_(*where_conditions))

        # 5. Group by and Having
        if plan.metrics and plan.dimensions:
            stmt = stmt.group_by(*group_by_exprs)
        elif plan.dimensions and not plan.metrics:
            # When querying only dimensions, apply distinct
            stmt = stmt.distinct()

        if having_conditions:
            stmt = stmt.having(sa.and_(*having_conditions))

        # 6. Order by
        if plan.order_by:
            order_exprs = []
            for ob in plan.order_by:
                target_expr = column_map.get(ob.field)
                if target_expr is None:
                    # Could be ordering by a declared dimension not in select list
                    if ob.field in cube.allowed_dimension_names:
                        target_expr = self._resolve_dimension_expr(ob.field, cube, main_table, target_tables)
                    else:
                        raise AllowlistError(
                            f"Order by field '{ob.field}' is not in selected dimensions/metrics "
                            f"or cube '{cube.name}'"
                        )
                if ob.direction == "desc":
                    order_exprs.append(target_expr.desc())
                else:
                    order_exprs.append(target_expr.asc())
            stmt = stmt.order_by(*order_exprs)

        # 7. Limit & Offset
        if plan.limit is not None:
            stmt = stmt.limit(plan.limit)
        if plan.offset is not None:
            stmt = stmt.offset(plan.offset)

        # 8. Compile to SQL string and params
        dialect_obj = _get_dialect(target_dialect_name)
        compiled = stmt.compile(dialect=dialect_obj, compile_kwargs={"render_postcompile": True})
        sql_str = str(compiled)
        params_dict = dict(compiled.params or {})

        return CompiledQuery(
            sql=sql_str,
            params=params_dict,
            dialect=target_dialect_name,
            statement=stmt,
        )

    def _validate_allowlists(self, plan: QueryPlan, cube: Cube) -> None:
        """Enforce strict allowlists on cube name, dimensions, metrics, joins, and filters."""
        if plan.cube != cube.name:
            raise AllowlistError(
                f"QueryPlan cube '{plan.cube}' does not match target cube '{cube.name}'"
            )

        # Dimensions allowlist
        for dim_name in plan.dimensions:
            if "." in dim_name:
                join_name, _ = dim_name.split(".", 1)
                if join_name not in cube.allowed_join_names:
                    raise AllowlistError(
                        f"Dimension '{dim_name}' references join '{join_name}' "
                        f"which is not allowed in cube '{cube.name}'"
                    )
            elif dim_name not in cube.allowed_dimension_names:
                raise AllowlistError(
                    f"Dimension '{dim_name}' is not declared in cube '{cube.name}'"
                )

        # Metrics allowlist
        for metric_name in plan.metrics:
            if metric_name not in cube.allowed_metric_names:
                raise AllowlistError(
                    f"Metric '{metric_name}' is not declared in cube '{cube.name}'"
                )

        # Joins allowlist
        for join_name in plan.joins:
            if join_name not in cube.allowed_join_names:
                raise AllowlistError(
                    f"Join '{join_name}' is not declared in cube '{cube.name}'"
                )

        # Filters allowlist
        for flt in plan.filters:
            if "." in flt.field:
                join_name, _ = flt.field.split(".", 1)
                if join_name not in cube.allowed_join_names:
                    raise AllowlistError(
                        f"Filter field '{flt.field}' references join '{join_name}' "
                        f"which is not allowed in cube '{cube.name}'"
                    )
            elif flt.field in cube.allowed_metric_names:
                # Allowed metric filter
                pass
            elif not cube.is_filter_allowed(flt.field, flt.operator):
                raise AllowlistError(
                    f"Filter on field '{flt.field}' with operator '{flt.operator}' "
                    f"is not in allowlist for cube '{cube.name}'"
                )

        # Time range allowlist
        if plan.time_range:
            tr_field = plan.time_range.field
            if "." in tr_field:
                join_name, _ = tr_field.split(".", 1)
                if join_name not in cube.allowed_join_names:
                    raise AllowlistError(
                        f"Time range field '{tr_field}' references undeclared join '{join_name}'"
                    )
            elif tr_field not in cube.allowed_dimension_names and tr_field not in cube.allowed_filter_names:
                raise AllowlistError(
                    f"Time range field '{tr_field}' is not an allowed dimension in cube '{cube.name}'"
                )

    def _resolve_dimension_expr(
        self,
        field_name: str,
        cube: Cube,
        main_table: sa.TableClause,
        target_tables: dict[str, sa.TableClause],
    ) -> sa.sql.ColumnElement[Any]:
        """Resolve a dimension name to a qualified SQLAlchemy column expression."""
        if "." in field_name:
            join_name, col_name = field_name.split(".", 1)
            tbl = target_tables.get(join_name)
            if tbl is None:
                tbl = sa.table(join_name)
                target_tables[join_name] = tbl
            return _get_table_col(tbl, col_name)

        dim = cube.get_dimension(field_name)
        col_name = dim.column if dim and dim.column else field_name
        if "." in col_name:
            tbl_name, c_name = col_name.split(".", 1)
            tbl = target_tables.get(tbl_name)
            if tbl is None:
                tbl = sa.table(tbl_name)
                target_tables[tbl_name] = tbl
            return _get_table_col(tbl, c_name)
        return _get_table_col(main_table, col_name)

    def _build_metric_expr(
        self,
        metric: Any,
        cube: Cube,
        main_table: sa.TableClause,
        target_tables: dict[str, sa.TableClause],
    ) -> sa.sql.ColumnElement[Any]:
        """Construct the aggregated SQLAlchemy expression for a metric."""
        agg = metric.aggregation
        col = metric.column

        if agg == "count":
            if col is None or col == "*":
                return sa.func.count()
            col_expr = self._resolve_column_ref(col, main_table, target_tables)
            return sa.func.count(col_expr)

        if col is None:
            raise CompilationError(f"Metric '{metric.name}' requires a column")

        col_expr = self._resolve_column_ref(col, main_table, target_tables)

        if agg == "count_distinct":
            return sa.func.count(sa.distinct(col_expr))
        if agg == "sum":
            return sa.func.sum(col_expr)
        if agg == "avg":
            return sa.func.avg(col_expr)
        if agg == "min":
            return sa.func.min(col_expr)
        if agg == "max":
            return sa.func.max(col_expr)

        raise CompilationError(f"Unsupported aggregation '{agg}' for metric '{metric.name}'")

    def _resolve_column_ref(
        self,
        col_ref: str,
        main_table: sa.TableClause,
        target_tables: dict[str, sa.TableClause],
    ) -> sa.sql.ColumnElement[Any]:
        """Resolve a column reference (col or table.col) to a TableClause column."""
        if "." in col_ref:
            tbl_name, c_name = col_ref.split(".", 1)
            tbl = target_tables.get(tbl_name)
            if tbl is None:
                tbl = sa.table(tbl_name)
                target_tables[tbl_name] = tbl
            return _get_table_col(tbl, c_name)
        return _get_table_col(main_table, col_ref)

    def _build_filter_condition(
        self,
        expr: sa.sql.ColumnElement[Any],
        flt: PlanFilter,
        idx: int,
    ) -> sa.sql.BinaryExpression[Any] | sa.sql.ColumnElement[Any]:
        """Build a parameterized SQLAlchemy condition for a filter."""
        op = flt.operator
        val = flt.value
        p_name = f"p_{idx}_{flt.field.replace('.', '_')}"

        if op == "eq":
            return expr == sa.bindparam(p_name, val)
        if op == "neq":
            return expr != sa.bindparam(p_name, val)
        if op == "gt":
            return expr > sa.bindparam(p_name, val)
        if op == "gte":
            return expr >= sa.bindparam(p_name, val)
        if op == "lt":
            return expr < sa.bindparam(p_name, val)
        if op == "lte":
            return expr <= sa.bindparam(p_name, val)
        if op == "like":
            return expr.like(sa.bindparam(p_name, val))
        if op == "not_like":
            return expr.not_like(sa.bindparam(p_name, val))
        if op == "in":
            return expr.in_(sa.bindparam(p_name, list(val), expanding=True))
        if op == "not_in":
            return expr.not_in(sa.bindparam(p_name, list(val), expanding=True))
        if op == "between":
            p_start = f"{p_name}_start"
            p_end = f"{p_name}_end"
            return expr.between(sa.bindparam(p_start, val[0]), sa.bindparam(p_end, val[1]))
        if op == "is_null":
            return expr.is_(None)
        if op == "is_not_null":
            return expr.is_not(None)

        raise CompilationError(f"Unsupported filter operator '{op}'")
