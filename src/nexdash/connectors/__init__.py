"""Pluggable data source connectors that feed the semantic layer.

Connectors read GitHub, Azure DevOps, Redmine, Jira or any JSON REST API, normalize
the payload into ``WorkItem`` rows and load them into the table of the
``work_items`` cube. Queries then go through the usual ``QueryPlan`` and guarded
SQL compiler, so the model never writes free SQL against connector data either.

New connectors subclass ``BaseConnector`` and call ``register_connector``.
"""

from __future__ import annotations

from typing import Any

from nexdash.connectors.azure_devops import AzureDevOpsConnector
from nexdash.connectors.base import (
    BaseConnector,
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorHTTPError,
    ConnectorRateLimitError,
    resolve_credential,
)
from nexdash.connectors.generic import GenericRestConnector
from nexdash.connectors.github import GitHubConnector
from nexdash.connectors.jira import JiraConnector
from nexdash.connectors.loader import DEFAULT_TABLE, load_work_items, sync_connector, work_items_cube
from nexdash.connectors.models import WORK_ITEM_FIELDS, WorkItem, join_labels, to_iso_utc
from nexdash.connectors.redmine import RedmineConnector

CONNECTORS: dict[str, type[BaseConnector]] = {}


def register_connector(cls: type[BaseConnector], *, kind: str | None = None) -> type[BaseConnector]:
    """Register a connector class under ``kind`` (defaults to ``cls.kind``)."""
    name = kind or cls.kind
    if not name or name == BaseConnector.kind:
        raise ConnectorConfigError("Connector classes need a distinct 'kind'.")
    CONNECTORS[name] = cls
    return cls


for _cls in (GitHubConnector, AzureDevOpsConnector, RedmineConnector, JiraConnector, GenericRestConnector):
    register_connector(_cls)


def build_connector(kind: str, **config: Any) -> BaseConnector:
    """Instantiate a registered connector from configuration (for example a parsed YAML block)."""
    try:
        cls = CONNECTORS[kind]
    except KeyError:
        raise ConnectorConfigError(
            f"Unknown connector '{kind}'. Available: {', '.join(sorted(CONNECTORS))}."
        ) from None
    for forbidden in ("token", "api_key", "password", "secret"):
        if forbidden in config:
            raise ConnectorConfigError(
                f"Do not put '{forbidden}' in connector configuration; name the variable with token_env."
            )
    return cls(**config)


__all__ = [
    "CONNECTORS",
    "DEFAULT_TABLE",
    "WORK_ITEM_FIELDS",
    "AzureDevOpsConnector",
    "BaseConnector",
    "ConnectorAuthError",
    "ConnectorConfigError",
    "ConnectorError",
    "ConnectorHTTPError",
    "ConnectorRateLimitError",
    "GenericRestConnector",
    "GitHubConnector",
    "JiraConnector",
    "RedmineConnector",
    "WorkItem",
    "build_connector",
    "join_labels",
    "load_work_items",
    "register_connector",
    "resolve_credential",
    "sync_connector",
    "to_iso_utc",
    "work_items_cube",
]
