"""GitHub issues and pull requests connector (REST API v3)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, ClassVar
from urllib.parse import quote

from nexdash.connectors.base import BaseConnector, ConnectorConfigError, ConnectorError
from nexdash.connectors.models import WorkItem

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_STATES = ("open", "closed", "all")


class GitHubConnector(BaseConnector):
    """Reads issues (and optionally pull requests) of one repository.

    Credential: a fine-grained or classic token in ``NEXDASH_GITHUB_TOKEN`` (configurable
    with ``token_env``), sent as ``Authorization: Bearer``. Pagination follows the
    ``Link: rel="next"`` header.
    """

    kind: ClassVar[str] = "github"
    default_token_env: ClassVar[str] = "NEXDASH_GITHUB_TOKEN"

    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        base_url: str = "https://api.github.com",
        token_env: str | None = None,
        state: str = "all",
        include_pull_requests: bool = True,
        **kwargs: Any,
    ) -> None:
        if not _NAME_RE.match(owner or "") or not _NAME_RE.match(repo or ""):
            raise ConnectorConfigError("GitHub owner and repo must contain only letters, digits, '.', '_' or '-'.")
        if state not in _STATES:
            raise ConnectorConfigError(f"GitHub state must be one of {', '.join(_STATES)}.")
        super().__init__(base_url, **kwargs)
        self.owner = owner
        self.repo = repo
        self.state = state
        self.include_pull_requests = include_pull_requests
        self.token_env = token_env or self.default_token_env

    def auth_headers(self) -> dict[str, str]:
        token = self._credential(self.token_env)
        return {"Authorization": f"Bearer {token}"}

    def default_headers(self) -> dict[str, str]:
        return {
            **super().default_headers(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def iter_items(self) -> Iterator[WorkItem]:
        url: str | None = self.url(f"repos/{quote(self.owner)}/{quote(self.repo)}/issues")
        params: dict[str, Any] | None = {
            "state": self.state,
            "per_page": min(self.page_size, 100),
            "sort": "created",
            "direction": "asc",
        }
        pages = 0
        while url and self._check_page_budget(pages):
            payload, response = self.get_json(url, params=params)
            pages += 1
            if not isinstance(payload, list):
                raise ConnectorError("github: expected a JSON array of issues.")
            for raw in payload:
                if "pull_request" in raw and not self.include_pull_requests:
                    continue
                yield self.normalize(raw)
            url = response.links.get("next", {}).get("url")
            params = None  # the next link already carries the query string

    def normalize(self, raw: dict[str, Any]) -> WorkItem:
        number = raw.get("number")
        is_pr = "pull_request" in raw
        assignee = (raw.get("assignee") or {}).get("login")
        author = (raw.get("user") or {}).get("login")
        state = "closed" if raw.get("state") == "closed" else "open"
        return WorkItem(
            source=self.kind,
            project=f"{self.owner}/{self.repo}",
            item_id=str(raw.get("id") or number),
            item_key=f"{self.repo}#{number}",
            title=raw.get("title"),
            item_type="pull_request" if is_pr else "issue",
            status=raw.get("state_reason") or raw.get("state"),
            state=state,
            assignee=assignee,
            author=author,
            labels=raw.get("labels") or [],
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            closed_at=raw.get("closed_at"),
            url=raw.get("html_url"),
        )
