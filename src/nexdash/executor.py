"""Query execution layer for compiled semantic queries and raw parameterized SQL.

Provides:
- ExecutionResult: structured query results with column metadata and row mappings.
- DatabaseExecutor / SQLAlchemyExecutor: pluggable executor running on any SQLAlchemy-supported
  database (SQLite, PostgreSQL, MySQL, Microsoft SQL Server, ODBC).
- SQLiteExecutor: lightweight executor for SQLite databases and connections.
- get_executor: unified factory resolving database configuration automatically.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from nexdash.compiler import CompiledQuery, SQLCompiler
from nexdash.cube import Cube, CubeRegistry
from nexdash.db import create_database_engine, get_db_url
from nexdash.plan import QueryPlan


@dataclass(frozen=True)
class ExecutionResult:
    """The result of executing a query, with column metadata and row mappings."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return self.row_count

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.rows[index]

    def column_values(self, column: str) -> list[Any]:
        """Return a list containing values for a single column across all rows."""
        if column not in self.columns:
            raise KeyError(f"Column '{column}' not in result columns: {self.columns}")
        return [row[column] for row in self.rows]


class DatabaseExecutor:
    """Executes compiled queries and parameterized SQL against any SQLAlchemy database."""

    def __init__(
        self,
        database: sa.Engine | str | Path | None = None,
        compiler: SQLCompiler | None = None,
        **engine_kwargs: Any,
    ) -> None:
        if isinstance(database, sa.Engine):
            self.engine = database
            self._owns_engine = False
        else:
            resolved_url = database if database is not None else get_db_url()
            self.engine = create_database_engine(resolved_url, **engine_kwargs)
            self._owns_engine = True

        self.dialect_name: str = self.engine.dialect.name
        self.compiler = compiler or SQLCompiler(default_dialect=self.dialect_name)

    def execute_compiled(self, compiled: CompiledQuery) -> ExecutionResult:
        """Execute a CompiledQuery with bound parameters and return structured results."""
        with self.engine.connect() as conn:
            stmt = sa.text(compiled.sql)
            result = conn.execute(stmt, compiled.params)
            columns = list(result.keys()) if result.returns_rows else []
            rows = [dict(row) for row in result.mappings()] if result.returns_rows else []
            return ExecutionResult(columns=columns, rows=rows, row_count=len(rows))

    def execute_plan(
        self,
        plan: QueryPlan,
        cube: Cube,
        registry: CubeRegistry | None = None,
    ) -> ExecutionResult:
        """Compile a plan for this executor's dialect and execute it against the database."""
        compiled = self.compiler.compile(plan, cube, registry=registry, dialect=self.dialect_name)
        return self.execute_compiled(compiled)

    def execute_sql(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Execute parameterized SQL query and return structured results."""
        with self.engine.connect() as conn:
            stmt = sa.text(sql)
            result = conn.execute(stmt, params or {})
            columns = list(result.keys()) if result.returns_rows else []
            rows = [dict(row) for row in result.mappings()] if result.returns_rows else []
            return ExecutionResult(columns=columns, rows=rows, row_count=len(rows))

    def execute_script(self, script: str) -> None:
        """Execute DDL statements or seed script against the database."""
        # Split script into individual statements by semicolon outside of strings
        statements = [stmt.strip() for stmt in re.split(r";\s*\n|;\s*$", script) if stmt.strip()]
        with self.engine.begin() as conn:
            for stmt_text in statements:
                conn.execute(sa.text(stmt_text))

    def load_records(self, table_name: str, records: list[dict[str, Any]]) -> None:
        """Create a table dynamically from records and insert rows with parameter binding."""
        if not records:
            return

        columns = list(records[0].keys())
        metadata = sa.MetaData()
        table = sa.Table(
            table_name,
            metadata,
            *(sa.Column(col, sa.Text) for col in columns),
        )
        metadata.create_all(self.engine)

        with self.engine.begin() as conn:
            conn.execute(table.insert(), records)

    def close(self) -> None:
        """Dispose of the underlying SQLAlchemy engine if owned by this instance."""
        if self._owns_engine:
            self.engine.dispose()

    def __enter__(self) -> DatabaseExecutor:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# Alias for explicitly naming SQLAlchemy backend
SQLAlchemyExecutor = DatabaseExecutor


class SQLiteExecutor:
    """Executes compiled queries directly against a SQLite connection or database file."""

    def __init__(
        self,
        database: sqlite3.Connection | str | Path | None = ":memory:",
        compiler: SQLCompiler | None = None,
    ) -> None:
        self.compiler = compiler or SQLCompiler(default_dialect="sqlite")
        self._owns_connection = False

        if isinstance(database, sqlite3.Connection):
            self.connection = database
        else:
            db_path = str(database) if database is not None else ":memory:"
            self.connection = sqlite3.connect(db_path)
            self._owns_connection = True

        self.connection.row_factory = sqlite3.Row

    def execute_compiled(self, compiled: CompiledQuery) -> ExecutionResult:
        """Execute a CompiledQuery with bound parameters and return structured results."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(compiled.sql, compiled.params)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            fetched_rows = cursor.fetchall()
            rows = [dict(zip(columns, row, strict=False)) for row in fetched_rows]
            return ExecutionResult(columns=columns, rows=rows, row_count=len(rows))
        finally:
            cursor.close()

    def execute_plan(
        self,
        plan: QueryPlan,
        cube: Cube,
        registry: CubeRegistry | None = None,
    ) -> ExecutionResult:
        """Compile a plan and execute it against the database."""
        compiled = self.compiler.compile(plan, cube, registry=registry, dialect="sqlite")
        return self.execute_compiled(compiled)

    def execute_script(self, script: str) -> None:
        """Execute raw DDL or seed script (useful for test setup)."""
        self.connection.executescript(script)
        self.connection.commit()

    def load_records(self, table_name: str, records: list[dict[str, Any]]) -> None:
        """Create a table dynamically from sample records and insert rows."""
        if not records:
            return

        columns = list(records[0].keys())
        col_list_sql = ", ".join(f'"{col}"' for col in columns)
        col_defs = ", ".join(f'"{col}" TEXT' for col in columns)
        create_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})'  # noqa: S608
        self.connection.execute(create_sql)

        placeholders = ", ".join(f":{col}" for col in columns)
        insert_sql = f'INSERT INTO "{table_name}" ({col_list_sql}) VALUES ({placeholders})'  # noqa: S608
        self.connection.executemany(insert_sql, records)
        self.connection.commit()

    def close(self) -> None:
        """Close the underlying connection if owned by this executor."""
        if self._owns_connection and self.connection:
            self.connection.close()

    def __enter__(self) -> SQLiteExecutor:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def get_executor(
    database: sa.Engine | sqlite3.Connection | str | Path | None = None,
    compiler: SQLCompiler | None = None,
    **engine_kwargs: Any,
) -> DatabaseExecutor | SQLiteExecutor:
    """Return an executor suited for the given database or connection.

    If database is an existing sqlite3.Connection, returns SQLiteExecutor.
    Otherwise returns a pluggable DatabaseExecutor.
    """
    if isinstance(database, sqlite3.Connection):
        return SQLiteExecutor(database, compiler=compiler)
    return DatabaseExecutor(database, compiler=compiler, **engine_kwargs)
