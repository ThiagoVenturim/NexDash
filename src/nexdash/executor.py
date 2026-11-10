"""SQLite query executor for compiled semantic queries.

Executes compiled parameterized queries against a SQLite database
and returns structured query results.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexdash.compiler import CompiledQuery, SQLCompiler
from nexdash.cube import Cube, CubeRegistry
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


class SQLiteExecutor:
    """Executes compiled queries against a SQLite database."""

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

        # Ensure foreign keys are enabled and rows return dict-accessible structures if needed
        self.connection.row_factory = sqlite3.Row

    def execute_compiled(self, compiled: CompiledQuery) -> ExecutionResult:
        """Execute a CompiledQuery with bound parameters and return structured results."""
        cursor = self.connection.cursor()
        try:
            # Parameters are bound directly, never string-interpolated
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
