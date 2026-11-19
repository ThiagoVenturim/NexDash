"""Load connector output into a database table described by the work_items cube.

Connectors only write rows. Reading goes through the semantic layer: a ``QueryPlan``
against the ``work_items`` cube, compiled by ``SQLCompiler`` into parameterized SQL.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa

from nexdash.connectors.base import BaseConnector
from nexdash.connectors.models import WORK_ITEM_FIELDS, WorkItem
from nexdash.cube import Cube, validate_identifier
from nexdash.executor import DatabaseExecutor, SQLiteExecutor

DEFAULT_TABLE = "work_items"

_DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("source", "string", "Connector that produced the item (github, jira, ...)"),
    ("project", "string", "Repository or project the item belongs to"),
    ("item_id", "string", "Identifier of the item in the source tool"),
    ("item_key", "string", "Human-readable key (repo#12, PROJ-7, #42)"),
    ("title", "string", "Title or summary"),
    ("item_type", "string", "Issue, bug, task, pull_request, ..."),
    ("status", "string", "Status name as shown in the source tool"),
    ("state", "string", "Normalized state: open or closed"),
    ("assignee", "string", "Current assignee"),
    ("author", "string", "Creator or reporter"),
    ("priority", "string", "Priority name or number"),
    ("labels", "string", "Comma-separated labels or tags"),
    ("created_at", "datetime", "Creation time, ISO 8601 UTC"),
    ("updated_at", "datetime", "Last update time, ISO 8601 UTC"),
    ("closed_at", "datetime", "Close or resolution time, ISO 8601 UTC"),
)


def work_items_cube(name: str = "work_items", source: str = DEFAULT_TABLE) -> Cube:
    """Return the cube describing the table that connectors populate."""
    return Cube.model_validate(
        {
            "name": name,
            "source": source,
            "description": "Work items (issues, tickets, tasks, pull requests) collected by connectors",
            "dimensions": [{"name": n, "type": t, "description": d} for n, t, d in _DIMENSIONS],
            "metrics": [
                {"name": "item_count", "aggregation": "count", "description": "Number of work items"},
                {
                    "name": "assignee_count",
                    "aggregation": "count_distinct",
                    "column": "assignee",
                    "description": "Distinct assignees",
                },
                {
                    "name": "avg_resolution_hours",
                    "aggregation": "avg",
                    "column": "resolution_hours",
                    "description": "Average hours from creation to close",
                },
                {
                    "name": "max_resolution_hours",
                    "aggregation": "max",
                    "column": "resolution_hours",
                    "description": "Longest time from creation to close, in hours",
                },
            ],
        }
    )


def work_items_table(table: str = DEFAULT_TABLE, metadata: sa.MetaData | None = None) -> sa.Table:
    """SQLAlchemy definition of the work items table (text columns, numeric resolution_hours)."""
    validate_identifier(table, "table name")
    columns = [
        sa.Column(name, sa.Float if name == "resolution_hours" else sa.Text) for name in WORK_ITEM_FIELDS
    ]
    return sa.Table(table, metadata or sa.MetaData(), *columns)


def _prepare_table(executor: DatabaseExecutor | SQLiteExecutor, table: str, replace: bool) -> None:
    if isinstance(executor, DatabaseExecutor):
        sa_table = work_items_table(table)
        if replace:
            sa_table.drop(executor.engine, checkfirst=True)
        sa_table.create(executor.engine, checkfirst=True)
        return
    column_defs = ", ".join(
        f'"{name}" REAL' if name == "resolution_hours" else f'"{name}" TEXT' for name in WORK_ITEM_FIELDS
    )
    script = f'CREATE TABLE IF NOT EXISTS "{table}" ({column_defs});'
    if replace:
        script = f'DROP TABLE IF EXISTS "{table}";\n' + script
    executor.execute_script(script)


def load_work_items(
    executor: DatabaseExecutor | SQLiteExecutor,
    items: Iterable[WorkItem],
    *,
    table: str = DEFAULT_TABLE,
    replace: bool = True,
) -> int:
    """Write work items to ``table`` with bound parameters. Returns the number of rows written.

    With ``replace=True`` the table is recreated first, so a refresh does not duplicate rows.
    The table name is checked against the identifier allowlist before any DDL runs.
    """
    validate_identifier(table, "table name")
    records: list[dict[str, Any]] = [item.to_record() for item in items]
    _prepare_table(executor, table, replace)
    executor.load_records(table, records)
    return len(records)


def sync_connector(
    connector: BaseConnector,
    executor: DatabaseExecutor | SQLiteExecutor,
    *,
    table: str = DEFAULT_TABLE,
    replace: bool = True,
    limit: int | None = None,
) -> int:
    """Fetch every item from ``connector`` and load it into ``table``."""
    return load_work_items(executor, connector.fetch_items(limit=limit), table=table, replace=replace)
