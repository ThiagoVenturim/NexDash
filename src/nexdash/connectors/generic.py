"""Configurable REST connector for other project management tools.

The configuration says where the items are in the JSON response, how to page
through results and which response fields map onto the ``WorkItem`` columns.
Field paths use the same dot syntax as ``nexdash.sources.extract_json_path``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar, Literal

from nexdash.connectors.base import BaseConnector, ConnectorConfigError, ConnectorError
from nexdash.connectors.models import WORK_ITEM_FIELDS, WorkItem
from nexdash.sources import SourceCollectionError, extract_json_path

PaginationStyle = Literal["none", "page", "offset", "cursor", "link"]
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_MAPPABLE = set(WORK_ITEM_FIELDS) - {"source", "state", "resolution_hours"}


def _lookup(item: Any, path: str) -> Any:
    try:
        return extract_json_path(item, path)
    except SourceCollectionError:
        return None


class GenericRestConnector(BaseConnector):
    """Reads work items from any JSON REST API described by configuration.

    Args:
        base_url: Origin and optional base path of the API.
        path: Endpoint path relative to ``base_url`` (for example ``api/v1/tasks``).
        field_map: WorkItem column -> dot path inside each item. ``item_id`` is required.
        items_path: Dot path to the list of items in the response ('' when the body is the list).
        pagination: ``none``, ``page`` (page number), ``offset``, ``cursor`` or ``link``
            (``Link: rel="next"`` header).
        token_env: Variable holding the credential; ``auth_header``/``auth_scheme`` choose
            how it is sent (``Authorization: Bearer <token>`` by default).
        closed_values: Status values (case-insensitive) that mean the item is closed.
        query: Extra fixed query parameters.
    """

    kind: ClassVar[str] = "generic"
    default_token_env: ClassVar[str] = "NEXDASH_GENERIC_TOKEN"

    def __init__(
        self,
        base_url: str,
        path: str,
        *,
        field_map: Mapping[str, str],
        items_path: str = "",
        pagination: PaginationStyle = "none",
        token_env: str | None = None,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        require_token: bool = True,
        source_name: str = "generic",
        closed_values: list[str] | tuple[str, ...] = ("closed", "done", "resolved"),
        query: Mapping[str, str] | None = None,
        page_param: str = "page",
        size_param: str = "per_page",
        start_page: int = 1,
        offset_param: str = "offset",
        cursor_param: str = "cursor",
        cursor_path: str = "next_cursor",
        total_path: str = "",
        **kwargs: Any,
    ) -> None:
        if pagination not in ("none", "page", "offset", "cursor", "link"):
            raise ConnectorConfigError(f"Unknown pagination style '{pagination}'.")
        if "item_id" not in field_map:
            raise ConnectorConfigError("field_map must map 'item_id'.")
        unknown = set(field_map) - _MAPPABLE
        if unknown:
            raise ConnectorConfigError("field_map has unknown columns: " + ", ".join(sorted(unknown)) + ".")
        if not _HEADER_RE.match(auth_header):
            raise ConnectorConfigError("auth_header must be a plain HTTP header name.")
        if not _SOURCE_RE.match(source_name):
            raise ConnectorConfigError("source_name must be lowercase letters, digits and '_'.")
        if "://" in path or path.startswith("//"):
            raise ConnectorConfigError("path must be relative to base_url.")
        super().__init__(base_url, **kwargs)
        self.path = path
        self.field_map = dict(field_map)
        self.items_path = items_path
        self.pagination = pagination
        self.token_env = token_env or self.default_token_env
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.require_token = require_token
        self.source_name = source_name
        self.closed_values = {v.lower() for v in closed_values}
        self.query = dict(query or {})
        self.page_param = page_param
        self.size_param = size_param
        self.start_page = start_page
        self.offset_param = offset_param
        self.cursor_param = cursor_param
        self.cursor_path = cursor_path
        self.total_path = total_path

    def auth_headers(self) -> dict[str, str]:
        token = self._credential(self.token_env, required=self.require_token)
        if not token:
            return {}
        value = f"{self.auth_scheme} {token}" if self.auth_scheme else token
        return {self.auth_header: value}

    def _items(self, payload: Any) -> list[Any]:
        items = _lookup(payload, self.items_path) if self.items_path else payload
        if not isinstance(items, list):
            raise ConnectorError(f"{self.source_name}: items_path '{self.items_path}' is not a list.")
        return items

    def iter_items(self) -> Iterator[WorkItem]:
        url: str | None = self.url(self.path)
        size = self.page_size
        page = self.start_page
        offset = 0
        cursor: str | None = None
        pages = 0
        while url and self._check_page_budget(pages):
            params: dict[str, Any] = dict(self.query)
            if self.pagination in ("page", "offset", "cursor"):
                params[self.size_param] = size
            if self.pagination == "page":
                params[self.page_param] = page
            elif self.pagination == "offset":
                params[self.offset_param] = offset
            elif self.pagination == "cursor" and cursor:
                params[self.cursor_param] = cursor
            payload, response = self.get_json(url, params=params if pages == 0 or self.pagination != "link" else None)
            pages += 1
            items = self._items(payload)
            for raw in items:
                yield self.normalize(raw)

            if self.pagination == "none" or not items:
                return
            if self.pagination == "link":
                url = response.links.get("next", {}).get("url")
                continue
            if self.pagination == "cursor":
                next_cursor = _lookup(payload, self.cursor_path)
                if not next_cursor:
                    return
                cursor = str(next_cursor)
                continue
            offset += len(items)
            page += 1
            total = _lookup(payload, self.total_path) if self.total_path else None
            if isinstance(total, int) and offset >= total:
                return
            if total is None and len(items) < size:
                return

    def normalize(self, raw: Any) -> WorkItem:
        values: dict[str, Any] = {name: _lookup(raw, path) for name, path in self.field_map.items()}
        if values.get("labels") is None:
            values["labels"] = []
        status = values.get("status")
        closed = (status is not None and str(status).lower() in self.closed_values) or bool(values.get("closed_at"))
        return WorkItem(source=self.source_name, state="closed" if closed else "open", **values)
