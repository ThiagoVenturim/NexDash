"""Azure DevOps Boards work item connector (WIQL + work items batch API)."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from typing import Any, ClassVar
from urllib.parse import quote

from nexdash.connectors.base import BaseConnector, ConnectorConfigError, ConnectorError
from nexdash.connectors.models import WorkItem

API_VERSION = "7.1"
MAX_BATCH = 200
_ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,49}$")
_PROJECT_RE = re.compile(r"^[^\\/:*?\"<>|#%{}+;$&]{1,64}$")
_WORK_ITEM_TYPE_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")

DEFAULT_CLOSED_STATES = ("Closed", "Done", "Removed", "Resolved", "Completed")

FIELDS = (
    "System.Id",
    "System.TeamProject",
    "System.Title",
    "System.WorkItemType",
    "System.State",
    "System.AssignedTo",
    "System.CreatedBy",
    "System.CreatedDate",
    "System.ChangedDate",
    "System.Tags",
    "Microsoft.VSTS.Common.ClosedDate",
    "Microsoft.VSTS.Common.Priority",
)


def _identity_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("displayName") or value.get("uniqueName")
    return value or None


class AzureDevOpsConnector(BaseConnector):
    """Reads work items of one Azure DevOps project.

    Credential: a personal access token in ``NEXDASH_AZURE_DEVOPS_TOKEN`` (configurable
    with ``token_env``), sent as HTTP Basic with an empty user name. A WIQL query lists
    the ids; the ids are then fetched in batches of ``page_size`` (at most 200).

    The WIQL text is fixed. The optional ``work_item_types`` filter is validated against
    a strict pattern before it is placed in the query, so configuration cannot inject WIQL.
    """

    kind: ClassVar[str] = "azure_devops"
    default_token_env: ClassVar[str] = "NEXDASH_AZURE_DEVOPS_TOKEN"

    def __init__(
        self,
        project: str,
        *,
        organization: str | None = None,
        base_url: str | None = None,
        token_env: str | None = None,
        work_item_types: list[str] | None = None,
        closed_states: tuple[str, ...] | list[str] = DEFAULT_CLOSED_STATES,
        max_items: int = 20000,
        **kwargs: Any,
    ) -> None:
        if not _PROJECT_RE.match(project or ""):
            raise ConnectorConfigError("Azure DevOps project name contains forbidden characters.")
        if base_url is None:
            if not organization or not _ORG_RE.match(organization):
                raise ConnectorConfigError("Azure DevOps needs a valid organization or an explicit base_url.")
            base_url = f"https://dev.azure.com/{organization}"
        for wit in work_item_types or []:
            if not _WORK_ITEM_TYPE_RE.match(wit):
                raise ConnectorConfigError(f"Invalid work item type '{wit}'.")
        super().__init__(base_url, **kwargs)
        self.project = project
        self.token_env = token_env or self.default_token_env
        self.work_item_types = list(work_item_types or [])
        self.closed_states = {s.lower() for s in closed_states}
        self.max_items = max_items

    def auth_headers(self) -> dict[str, str]:
        token = self._credential(self.token_env)
        encoded = base64.b64encode(f":{token}".encode()).decode("ascii")
        self._remember_secret(f"{self.token_env}_BASIC", encoded)
        return {"Authorization": f"Basic {encoded}"}

    def wiql(self) -> str:
        query = "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
        if self.work_item_types:
            quoted = ", ".join(f"'{wit}'" for wit in self.work_item_types)
            query += f" AND [System.WorkItemType] IN ({quoted})"
        return query + " ORDER BY [System.Id] ASC"

    def _project_path(self) -> str:
        return quote(self.project, safe="")

    def list_ids(self) -> list[int]:
        url = self.url(f"{self._project_path()}/_apis/wit/wiql")
        payload, _ = self.get_json(
            url,
            method="POST",
            params={"api-version": API_VERSION, "$top": self.max_items},
            json_body={"query": self.wiql()},
        )
        refs = payload.get("workItems") if isinstance(payload, dict) else None
        if not isinstance(refs, list):
            raise ConnectorError("azure_devops: WIQL response has no 'workItems' list.")
        return [int(ref["id"]) for ref in refs if isinstance(ref, dict) and "id" in ref]

    def iter_items(self) -> Iterator[WorkItem]:
        ids = self.list_ids()
        batch = min(self.page_size, MAX_BATCH)
        url = self.url(f"{self._project_path()}/_apis/wit/workitems")
        pages = 0
        for start in range(0, len(ids), batch):
            if not self._check_page_budget(pages):
                return
            chunk = ids[start : start + batch]
            payload, _ = self.get_json(
                url,
                params={
                    "ids": ",".join(str(i) for i in chunk),
                    "fields": ",".join(FIELDS),
                    "api-version": API_VERSION,
                    "errorPolicy": "omit",
                },
            )
            pages += 1
            values = payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(values, list):
                raise ConnectorError("azure_devops: work items response has no 'value' list.")
            for raw in values:
                if raw:
                    yield self.normalize(raw)

    def normalize(self, raw: dict[str, Any]) -> WorkItem:
        fields = raw.get("fields") or {}
        item_id = raw.get("id") or fields.get("System.Id")
        status = fields.get("System.State") or ""
        closed = status.lower() in self.closed_states
        priority = fields.get("Microsoft.VSTS.Common.Priority")
        return WorkItem(
            source=self.kind,
            project=fields.get("System.TeamProject") or self.project,
            item_id=str(item_id),
            item_key=f"{self.project}-{item_id}",
            title=fields.get("System.Title"),
            item_type=fields.get("System.WorkItemType"),
            status=status,
            state="closed" if closed else "open",
            assignee=_identity_name(fields.get("System.AssignedTo")),
            author=_identity_name(fields.get("System.CreatedBy")),
            priority=None if priority is None else str(priority),
            labels=fields.get("System.Tags") or "",
            created_at=fields.get("System.CreatedDate"),
            updated_at=fields.get("System.ChangedDate"),
            closed_at=fields.get("Microsoft.VSTS.Common.ClosedDate") if closed else None,
            url=f"{self.base_url}/{self._project_path()}/_workitems/edit/{item_id}",
        )
