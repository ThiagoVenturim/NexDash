"""Redmine issues connector (REST API, JSON format)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, ClassVar

from nexdash.connectors.base import BaseConnector, ConnectorConfigError, ConnectorError
from nexdash.connectors.models import WorkItem

MAX_LIMIT = 100
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    return None


class RedmineConnector(BaseConnector):
    """Reads issues of a Redmine instance, optionally limited to one project.

    Credential: the user API key in ``NEXDASH_REDMINE_API_KEY`` (configurable with
    ``token_env``), sent in the ``X-Redmine-API-Key`` header (never in the URL).
    Pagination uses ``offset``/``limit`` and the ``total_count`` of each response.
    """

    kind: ClassVar[str] = "redmine"
    default_token_env: ClassVar[str] = "NEXDASH_REDMINE_API_KEY"

    def __init__(
        self,
        base_url: str,
        *,
        project: str | None = None,
        token_env: str | None = None,
        status: str = "*",
        **kwargs: Any,
    ) -> None:
        if project is not None and not _PROJECT_RE.match(project):
            raise ConnectorConfigError("Redmine project identifier must be lowercase letters, digits, '-' or '_'.")
        if status not in ("*", "open", "closed"):
            raise ConnectorConfigError("Redmine status must be '*', 'open' or 'closed'.")
        super().__init__(base_url, **kwargs)
        self.project = project
        self.status = status
        self.token_env = token_env or self.default_token_env

    def auth_headers(self) -> dict[str, str]:
        return {"X-Redmine-API-Key": self._credential(self.token_env) or ""}

    def iter_items(self) -> Iterator[WorkItem]:
        url = self.url("issues.json")
        limit = min(self.page_size, MAX_LIMIT)
        offset = 0
        pages = 0
        while self._check_page_budget(pages):
            params: dict[str, Any] = {
                "status_id": self.status,
                "offset": offset,
                "limit": limit,
                "sort": "id",
            }
            if self.project:
                params["project_id"] = self.project
            payload, _ = self.get_json(url, params=params)
            pages += 1
            issues = payload.get("issues") if isinstance(payload, dict) else None
            if not isinstance(issues, list):
                raise ConnectorError("redmine: response has no 'issues' list.")
            for raw in issues:
                yield self.normalize(raw)
            offset += len(issues)
            total = payload.get("total_count")
            if not issues or not isinstance(total, int) or offset >= total:
                return

    def normalize(self, raw: dict[str, Any]) -> WorkItem:
        status = raw.get("status") or {}
        closed_on = raw.get("closed_on")
        is_closed = status.get("is_closed")
        closed = bool(is_closed) if is_closed is not None else bool(closed_on)
        item_id = raw.get("id")
        return WorkItem(
            source=self.kind,
            project=_name(raw.get("project")) or (self.project or ""),
            item_id=str(item_id),
            item_key=f"#{item_id}",
            title=raw.get("subject"),
            item_type=_name(raw.get("tracker")),
            status=status.get("name"),
            state="closed" if closed else "open",
            assignee=_name(raw.get("assigned_to")),
            author=_name(raw.get("author")),
            priority=_name(raw.get("priority")),
            labels=[],
            created_at=raw.get("created_on"),
            updated_at=raw.get("updated_on"),
            closed_at=closed_on if closed else None,
            url=f"{self.base_url}/issues/{item_id}",
        )
