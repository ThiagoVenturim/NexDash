"""Jira issues connector (Cloud enhanced search or Data Center/Server search)."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from typing import Any, ClassVar, Literal

from nexdash.connectors.base import BaseConnector, ConnectorConfigError, ConnectorError
from nexdash.connectors.models import WorkItem

MAX_RESULTS = 100
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
FIELDS = (
    "summary",
    "issuetype",
    "status",
    "assignee",
    "reporter",
    "priority",
    "labels",
    "created",
    "updated",
    "resolutiondate",
    "project",
)

JiraFlavor = Literal["cloud", "server"]


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("displayName") or value.get("name")
    return None


class JiraConnector(BaseConnector):
    """Reads issues of one Jira project (or of a custom JQL query).

    ``flavor="cloud"`` uses ``/rest/api/3/search/jql`` with ``nextPageToken`` and HTTP
    Basic auth built from ``NEXDASH_JIRA_EMAIL`` and ``NEXDASH_JIRA_API_TOKEN``.
    ``flavor="server"`` (Data Center/Server) uses ``/rest/api/2/search`` with
    ``startAt``/``total`` and a personal access token sent as Bearer.
    """

    kind: ClassVar[str] = "jira"
    default_token_env: ClassVar[str] = "NEXDASH_JIRA_API_TOKEN"
    default_email_env: ClassVar[str] = "NEXDASH_JIRA_EMAIL"

    def __init__(
        self,
        base_url: str,
        *,
        project: str | None = None,
        jql: str | None = None,
        flavor: JiraFlavor = "cloud",
        token_env: str | None = None,
        email_env: str | None = None,
        **kwargs: Any,
    ) -> None:
        if flavor not in ("cloud", "server"):
            raise ConnectorConfigError("Jira flavor must be 'cloud' or 'server'.")
        if jql is None:
            if not project or not _PROJECT_KEY_RE.match(project):
                raise ConnectorConfigError("Jira needs a project key (uppercase, e.g. 'PROJ') or an explicit jql.")
            jql = f"project = {project} ORDER BY created ASC"
        super().__init__(base_url, **kwargs)
        self.project = project
        self.jql = jql
        self.flavor = flavor
        self.token_env = token_env or self.default_token_env
        self.email_env = email_env or self.default_email_env

    def auth_headers(self) -> dict[str, str]:
        token = self._credential(self.token_env)
        if self.flavor == "server":
            return {"Authorization": f"Bearer {token}"}
        email = self._credential(self.email_env)
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        self._remember_secret(f"{self.token_env}_BASIC", encoded)
        return {"Authorization": f"Basic {encoded}"}

    def iter_items(self) -> Iterator[WorkItem]:
        if self.flavor == "cloud":
            yield from self._iter_cloud()
        else:
            yield from self._iter_server()

    def _issues(self, payload: Any) -> list[dict[str, Any]]:
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise ConnectorError("jira: search response has no 'issues' list.")
        return issues

    def _iter_cloud(self) -> Iterator[WorkItem]:
        url = self.url("rest/api/3/search/jql")
        token: str | None = None
        pages = 0
        while self._check_page_budget(pages):
            params: dict[str, Any] = {
                "jql": self.jql,
                "fields": ",".join(FIELDS),
                "maxResults": min(self.page_size, MAX_RESULTS),
            }
            if token:
                params["nextPageToken"] = token
            payload, _ = self.get_json(url, params=params)
            pages += 1
            for raw in self._issues(payload):
                yield self.normalize(raw)
            token = payload.get("nextPageToken")
            if payload.get("isLast", not token) or not token:
                return

    def _iter_server(self) -> Iterator[WorkItem]:
        url = self.url("rest/api/2/search")
        start_at = 0
        pages = 0
        while self._check_page_budget(pages):
            payload, _ = self.get_json(
                url,
                params={
                    "jql": self.jql,
                    "fields": ",".join(FIELDS),
                    "startAt": start_at,
                    "maxResults": min(self.page_size, MAX_RESULTS),
                },
            )
            pages += 1
            issues = self._issues(payload)
            for raw in issues:
                yield self.normalize(raw)
            start_at += len(issues)
            total = payload.get("total")
            if not issues or not isinstance(total, int) or start_at >= total:
                return

    def normalize(self, raw: dict[str, Any]) -> WorkItem:
        fields = raw.get("fields") or {}
        status = fields.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key")
        resolved = fields.get("resolutiondate")
        closed = category == "done" if category else bool(resolved)
        key = raw.get("key") or ""
        project = fields.get("project") or {}
        return WorkItem(
            source=self.kind,
            project=project.get("key") or self.project or "",
            item_id=str(raw.get("id") or key),
            item_key=key,
            title=fields.get("summary"),
            item_type=_name(fields.get("issuetype")),
            status=status.get("name"),
            state="closed" if closed else "open",
            assignee=_name(fields.get("assignee")),
            author=_name(fields.get("reporter")),
            priority=_name(fields.get("priority")),
            labels=fields.get("labels") or [],
            created_at=fields.get("created"),
            updated_at=fields.get("updated"),
            closed_at=resolved if closed else None,
            url=f"{self.base_url}/browse/{key}" if key else None,
        )
