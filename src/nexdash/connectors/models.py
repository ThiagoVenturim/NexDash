"""Normalized data model shared by every connector.

Each connector turns the native payload of its tool (GitHub issues, Azure DevOps
work items, Redmine issues, Jira issues, or any REST API) into ``WorkItem`` rows.
The rows map one to one onto the ``work_items`` cube, so the semantic layer and
its guarded SQL compiler can query them like any other table.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

WorkItemState = Literal["open", "closed"]

WORK_ITEM_FIELDS: tuple[str, ...] = (
    "source",
    "project",
    "item_id",
    "item_key",
    "title",
    "item_type",
    "status",
    "state",
    "assignee",
    "author",
    "priority",
    "labels",
    "created_at",
    "updated_at",
    "closed_at",
    "resolution_hours",
    "url",
)


def to_iso_utc(value: Any) -> str | None:
    """Normalize a timestamp to ISO 8601 in UTC (``YYYY-MM-DDTHH:MM:SSZ``).

    Accepts datetime/date objects, ISO strings with ``Z``, ``+00:00`` or ``+0000``
    offsets and optional fractions of a second. Naive values are treated as UTC.
    Returns None for empty values and raises ValueError for unparseable input.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Unrecognized timestamp '{value}'") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def join_labels(values: Iterable[Any] | str | None) -> str:
    """Join label names into a sorted, comma-separated string without duplicates."""
    if values is None:
        return ""
    if isinstance(values, str):
        raw = [part for part in values.replace(";", ",").split(",")]
    else:
        raw = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("name") or value.get("value") or value.get("title")
            if value is not None:
                raw.append(str(value))
    cleaned = {part.strip() for part in raw if part and part.strip()}
    return ",".join(sorted(cleaned))


def _hours_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    delta = datetime.strptime(end, fmt) - datetime.strptime(start, fmt)
    hours = delta.total_seconds() / 3600.0
    return round(hours, 2) if hours >= 0 else None


class WorkItem(BaseModel):
    """One work item (issue, ticket, task, pull request) in the normalized model."""

    source: str
    project: str = ""
    item_id: str
    item_key: str = ""
    title: str = ""
    item_type: str = ""
    status: str = ""
    state: WorkItemState = "open"
    assignee: str | None = None
    author: str | None = None
    priority: str | None = None
    labels: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    resolution_hours: float | None = None
    url: str | None = None

    @field_validator("item_id", "item_key", "project", "title", "item_type", "status", mode="before")
    @classmethod
    def _coerce_text(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("assignee", "author", "priority", mode="before")
    @classmethod
    def _coerce_optional_text(cls, v: Any) -> str | None:
        if v in (None, ""):
            return None
        return str(v)

    @field_validator("labels", mode="before")
    @classmethod
    def _coerce_labels(cls, v: Any) -> str:
        return join_labels(v)

    @field_validator("created_at", "updated_at", "closed_at", mode="before")
    @classmethod
    def _coerce_timestamp(cls, v: Any) -> str | None:
        return to_iso_utc(v)

    @model_validator(mode="after")
    def _derive(self) -> WorkItem:
        if not self.item_id:
            raise ValueError("WorkItem requires a non-empty item_id")
        if not self.item_key:
            self.item_key = self.item_id
        if self.closed_at and self.state == "open":
            self.state = "closed"
        if self.resolution_hours is None and self.state == "closed":
            self.resolution_hours = _hours_between(self.created_at, self.closed_at)
        return self

    def to_record(self) -> dict[str, Any]:
        """Return a flat dict with every column of the work_items table."""
        data = self.model_dump()
        return {name: data[name] for name in WORK_ITEM_FIELDS}
