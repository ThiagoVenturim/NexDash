"""Offline tests for the data source connectors.

Every HTTP exchange is served by ``httpx.MockTransport`` with synthetic payloads,
and DNS is replaced by a fake resolver, so no test touches the network.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from nexdash.compiler import AllowlistError
from nexdash.connectors import (
    CONNECTORS,
    AzureDevOpsConnector,
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorHTTPError,
    ConnectorRateLimitError,
    GenericRestConnector,
    GitHubConnector,
    JiraConnector,
    RedmineConnector,
    WorkItem,
    build_connector,
    join_labels,
    load_work_items,
    resolve_credential,
    sync_connector,
    to_iso_utc,
    work_items_cube,
)
from nexdash.cube import load_cube_from_yaml
from nexdash.executor import DatabaseExecutor, SQLiteExecutor
from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan

FAKE_TOKEN = "tok-FAKE-1234567890"


def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


class Recorder:
    """MockTransport handler that replays queued responses and records requests."""

    def __init__(self, responses: list[httpx.Response] | None = None, handler=None) -> None:
        self.responses = list(responses or [])
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        if not self.responses:
            raise AssertionError(f"Unexpected request {request.method} {request.url}")
        return self.responses.pop(0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def params_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


def common(recorder: Recorder, env: dict[str, str], **extra):
    return {"transport": recorder.transport, "env": env, "resolver": public_resolver, "sleep": lambda s: None, **extra}


# ---------------------------------------------------------------------------
# Normalized model
# ---------------------------------------------------------------------------


def test_to_iso_utc_accepts_common_formats():
    assert to_iso_utc("2026-01-02T10:00:00Z") == "2026-01-02T10:00:00Z"
    assert to_iso_utc("2026-01-02T10:00:00.123+0000") == "2026-01-02T10:00:00Z"
    assert to_iso_utc("2026-01-02T07:00:00-03:00") == "2026-01-02T10:00:00Z"
    assert to_iso_utc("2026-01-02T10:00:00.4567Z") == "2026-01-02T10:00:00Z"
    assert to_iso_utc(None) is None
    assert to_iso_utc("") is None
    with pytest.raises(ValueError):
        to_iso_utc("yesterday")


def test_join_labels_dedupes_and_sorts():
    assert join_labels(["bug", {"name": "ui"}, "bug", " "]) == "bug,ui"
    assert join_labels("b; a; b") == "a,b"
    assert join_labels(None) == ""


def test_work_item_derives_state_and_resolution_hours():
    item = WorkItem(
        source="x",
        item_id=7,
        created_at="2026-01-01T00:00:00Z",
        closed_at="2026-01-02T12:00:00Z",
    )
    assert item.state == "closed"
    assert item.item_key == "7"
    assert item.resolution_hours == 36.0
    assert set(item.to_record()) >= {"item_id", "state", "resolution_hours"}
    with pytest.raises(ValueError):
        WorkItem(source="x", item_id="")


# ---------------------------------------------------------------------------
# Credentials and base guards
# ---------------------------------------------------------------------------


def test_resolve_credential_names_variable_but_never_value():
    assert resolve_credential("MY_TOKEN", env={"MY_TOKEN": " abc "}) == "abc"
    with pytest.raises(ConnectorConfigError, match="MY_TOKEN"):
        resolve_credential("MY_TOKEN", env={})
    assert resolve_credential("MY_TOKEN", env={}, required=False) is None


def test_missing_token_raises_config_error():
    rec = Recorder([httpx.Response(200, json=[])])
    conn = GitHubConnector("acme", "widgets", **common(rec, {}))
    with pytest.raises(ConnectorConfigError, match="NEXDASH_GITHUB_TOKEN"):
        conn.fetch_items()
    assert rec.requests == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://169.254.169.254", "http://localhost"])
def test_base_url_ssrf_guard(url):
    with pytest.raises(ConnectorConfigError):
        RedmineConnector(url, env={})


def test_base_url_rejects_inline_credentials():
    with pytest.raises(ConnectorConfigError):
        RedmineConnector("https://user:pass@redmine.example.com", env={}, resolver=public_resolver)


def test_build_connector_registry_and_secret_rejection():
    assert set(CONNECTORS) == {"github", "azure_devops", "redmine", "jira", "generic"}
    conn = build_connector("redmine", base_url="https://redmine.example.com", env={}, resolver=public_resolver)
    assert isinstance(conn, RedmineConnector)
    with pytest.raises(ConnectorConfigError, match="Unknown connector"):
        build_connector("trello")
    with pytest.raises(ConnectorConfigError, match="token_env"):
        build_connector("github", owner="a", repo="b", token=FAKE_TOKEN)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def gh_issue(number: int, **extra) -> dict:
    data = {
        "id": 1000 + number,
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "user": {"login": "alice"},
        "assignee": {"login": "bob"},
        "labels": [{"name": "bug"}, {"name": "backend"}],
        "created_at": "2026-03-01T10:00:00Z",
        "updated_at": "2026-03-02T10:00:00Z",
        "closed_at": None,
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
    }
    data.update(extra)
    return data


def test_github_paginates_with_link_header_and_normalizes():
    next_url = "https://api.github.com/repositories/1/issues?state=all&per_page=2&page=2"
    rec = Recorder(
        [
            httpx.Response(200, json=[gh_issue(1), gh_issue(2, pull_request={"url": "x"})],
                           headers={"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'}),
            httpx.Response(200, json=[gh_issue(3, state="closed", state_reason="completed",
                                               closed_at="2026-03-01T22:00:00Z")]),
        ]
    )
    conn = GitHubConnector("acme", "widgets", page_size=2, **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    items = conn.fetch_items()

    assert [i.item_key for i in items] == ["widgets#1", "widgets#2", "widgets#3"]
    assert len(rec.requests) == 2
    first = rec.requests[0]
    assert first.url.path == "/repos/acme/widgets/issues"
    assert params_of(first)["per_page"] == "2"
    assert params_of(first)["state"] == "all"
    assert first.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert str(rec.requests[1].url) == next_url

    assert items[0].item_type == "issue"
    assert items[0].labels == "backend,bug"
    assert items[0].assignee == "bob"
    assert items[0].author == "alice"
    assert items[0].project == "acme/widgets"
    assert items[1].item_type == "pull_request"
    assert items[2].state == "closed"
    assert items[2].status == "completed"
    assert items[2].resolution_hours == 12.0


def test_github_can_skip_pull_requests():
    rec = Recorder([httpx.Response(200, json=[gh_issue(1), gh_issue(2, pull_request={})])])
    conn = GitHubConnector("acme", "widgets", include_pull_requests=False,
                           **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    assert [i.item_key for i in conn.fetch_items()] == ["widgets#1"]


def test_github_rejects_invalid_names():
    with pytest.raises(ConnectorConfigError):
        GitHubConnector("acme/../x", "widgets", env={}, resolver=public_resolver)


def test_rate_limit_retries_after_retry_after_header():
    sleeps: list[float] = []
    rec = Recorder(
        [
            httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "Retry-After": "2"}),
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json=[gh_issue(1)]),
        ]
    )
    conn = GitHubConnector("acme", "widgets", **{**common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}),
                                                  "sleep": sleeps.append})
    assert len(conn.fetch_items()) == 1
    assert sleeps == [2.0, 1.0]


def test_rate_limit_uses_reset_header_and_gives_up():
    rec = Recorder(handler=lambda r: httpx.Response(429, headers={"X-RateLimit-Reset": "1005"}))
    sleeps: list[float] = []
    conn = GitHubConnector(
        "acme", "widgets", max_retries=2,
        **{**common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}), "sleep": sleeps.append, "clock": lambda: 1000.0},
    )
    with pytest.raises(ConnectorRateLimitError) as info:
        conn.fetch_items()
    assert sleeps == [5.0, 5.0]
    assert len(rec.requests) == 3
    assert info.value.retry_after_s == 5.0


def test_rate_limit_longer_than_max_wait_fails_fast():
    rec = Recorder(handler=lambda r: httpx.Response(429, headers={"Retry-After": "3600"}))
    conn = GitHubConnector("acme", "widgets", **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    with pytest.raises(ConnectorRateLimitError):
        conn.fetch_items()
    assert len(rec.requests) == 1


def test_auth_error_does_not_leak_token(caplog):
    caplog.set_level(logging.DEBUG, logger="nexdash.connectors")
    rec = Recorder([httpx.Response(401, json={"message": "Bad credentials"})])
    conn = GitHubConnector("acme", "widgets", **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    with pytest.raises(ConnectorAuthError) as info:
        conn.fetch_items()
    assert FAKE_TOKEN not in str(info.value)
    assert FAKE_TOKEN not in caplog.text
    assert "/repos/acme/widgets/issues" in caplog.text


def test_transport_error_message_is_masked():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed with header {request.headers['Authorization']}")

    conn = GitHubConnector("acme", "widgets", **common(Recorder(handler=boom), {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    with pytest.raises(ConnectorError) as info:
        conn.fetch_items()
    assert FAKE_TOKEN not in str(info.value)
    assert "****" in str(info.value)


def test_next_link_to_other_host_is_refused():
    rec = Recorder(
        [httpx.Response(200, json=[gh_issue(1)], headers={"Link": '<https://evil.example.net/x>; rel="next"'})]
    )
    conn = GitHubConnector("acme", "widgets", **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    with pytest.raises(ConnectorError, match="outside the configured base URL"):
        conn.fetch_items()
    assert len(rec.requests) == 1


def test_redirects_are_not_followed():
    rec = Recorder([httpx.Response(302, headers={"Location": "https://api.github.com/other"})])
    conn = GitHubConnector("acme", "widgets", **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    with pytest.raises(ConnectorHTTPError) as info:
        conn.fetch_items()
    assert info.value.status_code == 302
    assert len(rec.requests) == 1


def test_server_error_and_invalid_json():
    rec = Recorder([httpx.Response(500), httpx.Response(200, content=b"<html>")])
    conn = GitHubConnector("acme", "widgets", cache_ttl_s=0, **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    with pytest.raises(ConnectorHTTPError, match="HTTP 500"):
        conn.fetch_items()
    with pytest.raises(ConnectorError, match="valid JSON"):
        conn.fetch_items()


def test_get_responses_are_cached_within_ttl():
    rec = Recorder(handler=lambda r: httpx.Response(200, json=[gh_issue(1)]))
    now = [1000.0]
    conn = GitHubConnector(
        "acme", "widgets", cache_ttl_s=60,
        **{**common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}), "clock": lambda: now[0]},
    )
    conn.fetch_items()
    conn.fetch_items()
    assert len(rec.requests) == 1
    now[0] += 61
    conn.fetch_items()
    assert len(rec.requests) == 2


def test_max_pages_marks_truncation():
    link = '<https://api.github.com/repos/acme/widgets/issues?page=2>; rel="next"'
    rec = Recorder(handler=lambda r: httpx.Response(200, json=[gh_issue(1)], headers={"Link": link}))
    conn = GitHubConnector("acme", "widgets", max_pages=3, cache_ttl_s=0,
                           **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    assert len(conn.fetch_items()) == 3
    assert conn.truncated is True


def test_fetch_items_limit_stops_early():
    link = '<https://api.github.com/repos/acme/widgets/issues?page=2>; rel="next"'
    rec = Recorder(handler=lambda r: httpx.Response(200, json=[gh_issue(1), gh_issue(2)], headers={"Link": link}))
    conn = GitHubConnector("acme", "widgets", **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    assert len(conn.fetch_items(limit=2)) == 2
    assert len(rec.requests) == 1


# ---------------------------------------------------------------------------
# Azure DevOps
# ---------------------------------------------------------------------------


def ado_item(item_id: int, state: str = "Active", **fields) -> dict:
    base = {
        "System.Id": item_id,
        "System.TeamProject": "Shop",
        "System.Title": f"Item {item_id}",
        "System.WorkItemType": "Bug",
        "System.State": state,
        "System.AssignedTo": {"displayName": "Carol", "uniqueName": "carol@example.com"},
        "System.CreatedBy": {"displayName": "Dave"},
        "System.CreatedDate": "2026-04-01T08:00:00.000Z",
        "System.ChangedDate": "2026-04-02T08:00:00.000Z",
        "System.Tags": "api; urgent",
        "Microsoft.VSTS.Common.Priority": 2,
    }
    base.update(fields)
    return {"id": item_id, "fields": base}


def test_azure_devops_wiql_then_batches():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"workItems": [{"id": 1}, {"id": 2}, {"id": 3}]})
        ids = [int(i) for i in params_of(request)["ids"].split(",")]
        values = [
            ado_item(i, state="Closed", **{"Microsoft.VSTS.Common.ClosedDate": "2026-04-01T20:00:00Z"})
            if i == 3 else ado_item(i)
            for i in ids
        ]
        return httpx.Response(200, json={"count": len(values), "value": values})

    rec = Recorder(handler=handler)
    conn = AzureDevOpsConnector(
        "My Shop", organization="acme", page_size=2, work_item_types=["Bug", "User Story"],
        **common(rec, {"NEXDASH_AZURE_DEVOPS_TOKEN": FAKE_TOKEN}),
    )
    items = conn.fetch_items()

    assert [i.item_id for i in items] == ["1", "2", "3"]
    wiql_req, batch1, batch2 = rec.requests
    assert wiql_req.method == "POST"
    assert wiql_req.url.raw_path.startswith(b"/acme/My%20Shop/_apis/wit/wiql?")
    query = json.loads(wiql_req.content)["query"]
    assert "@project" in query and "IN ('Bug', 'User Story')" in query
    auth = wiql_req.headers["Authorization"]
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth[6:]).decode() == f":{FAKE_TOKEN}"
    assert params_of(batch1)["ids"] == "1,2"
    assert params_of(batch2)["ids"] == "3"
    assert params_of(batch1)["api-version"] == "7.1"

    assert items[0].assignee == "Carol"
    assert items[0].author == "Dave"
    assert items[0].labels == "api,urgent"
    assert items[0].priority == "2"
    assert items[0].state == "open"
    assert items[0].created_at == "2026-04-01T08:00:00Z"
    assert items[2].state == "closed"
    assert items[2].resolution_hours == 12.0
    assert items[2].url == "https://dev.azure.com/acme/My%20Shop/_workitems/edit/3"


def test_azure_devops_validates_inputs():
    with pytest.raises(ConnectorConfigError):
        AzureDevOpsConnector("Shop", env={}, resolver=public_resolver)
    with pytest.raises(ConnectorConfigError):
        AzureDevOpsConnector("Shop", organization="acme", work_item_types=["Bug') OR 1=1 --"], env={},
                             resolver=public_resolver)


def test_azure_devops_bad_payload():
    rec = Recorder([httpx.Response(200, json={"unexpected": True})])
    conn = AzureDevOpsConnector("Shop", organization="acme", **common(rec, {"NEXDASH_AZURE_DEVOPS_TOKEN": "x"}))
    with pytest.raises(ConnectorError, match="workItems"):
        conn.fetch_items()


# ---------------------------------------------------------------------------
# Redmine
# ---------------------------------------------------------------------------


def rm_issue(issue_id: int, closed: bool = False) -> dict:
    return {
        "id": issue_id,
        "project": {"id": 1, "name": "Website"},
        "tracker": {"id": 1, "name": "Bug"},
        "status": {"id": 5 if closed else 1, "name": "Closed" if closed else "New", "is_closed": closed},
        "priority": {"id": 2, "name": "Normal"},
        "author": {"id": 3, "name": "Erin"},
        "assigned_to": {"id": 4, "name": "Frank"},
        "subject": f"Issue {issue_id}",
        "created_on": "2026-05-01T09:00:00Z",
        "updated_on": "2026-05-01T10:00:00Z",
        "closed_on": "2026-05-01T15:00:00Z" if closed else None,
    }


def test_redmine_offset_pagination_and_api_key_header():
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(params_of(request)["offset"])
        issues = [rm_issue(1), rm_issue(2)] if offset == 0 else [rm_issue(3, closed=True)]
        return httpx.Response(200, json={"issues": issues, "total_count": 3, "offset": offset, "limit": 2})

    rec = Recorder(handler=handler)
    conn = RedmineConnector("https://redmine.example.com/", project="website", page_size=2,
                            **common(rec, {"NEXDASH_REDMINE_API_KEY": FAKE_TOKEN}))
    items = conn.fetch_items()

    assert [i.item_key for i in items] == ["#1", "#2", "#3"]
    assert len(rec.requests) == 2
    first = rec.requests[0]
    assert first.url.path == "/issues.json"
    assert first.headers["X-Redmine-API-Key"] == FAKE_TOKEN
    assert FAKE_TOKEN not in str(first.url)
    assert params_of(first) == {"status_id": "*", "offset": "0", "limit": "2", "sort": "id", "project_id": "website"}
    assert params_of(rec.requests[1])["offset"] == "2"

    assert items[0].project == "Website"
    assert items[0].item_type == "Bug"
    assert items[0].priority == "Normal"
    assert items[0].assignee == "Frank"
    assert items[2].state == "closed"
    assert items[2].resolution_hours == 6.0
    assert items[2].url == "https://redmine.example.com/issues/3"


def test_redmine_closed_without_is_closed_flag():
    raw = rm_issue(9, closed=True)
    raw["status"].pop("is_closed")
    conn = RedmineConnector("https://redmine.example.com", env={}, resolver=public_resolver)
    assert conn.normalize(raw).state == "closed"


def test_redmine_rejects_bad_project():
    with pytest.raises(ConnectorConfigError):
        RedmineConnector("https://redmine.example.com", project="a&b=c", env={}, resolver=public_resolver)


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


def jira_issue(num: int, done: bool = False) -> dict:
    return {
        "id": str(10000 + num),
        "key": f"PROJ-{num}",
        "fields": {
            "summary": f"Story {num}",
            "issuetype": {"name": "Story"},
            "status": {"name": "Done" if done else "In Progress",
                       "statusCategory": {"key": "done" if done else "indeterminate"}},
            "assignee": {"displayName": "Grace"},
            "reporter": {"displayName": "Heidi"},
            "priority": {"name": "High"},
            "labels": ["frontend", "q2"],
            "created": "2026-06-01T09:00:00.000+0000",
            "updated": "2026-06-02T09:00:00.000+0000",
            "resolutiondate": "2026-06-02T09:00:00.000+0000" if done else None,
            "project": {"key": "PROJ"},
        },
    }


def test_jira_cloud_next_page_token():
    rec = Recorder(
        [
            httpx.Response(200, json={"issues": [jira_issue(1), jira_issue(2)], "nextPageToken": "abc",
                                      "isLast": False}),
            httpx.Response(200, json={"issues": [jira_issue(3, done=True)], "isLast": True}),
        ]
    )
    env = {"NEXDASH_JIRA_API_TOKEN": FAKE_TOKEN, "NEXDASH_JIRA_EMAIL": "bot@example.com"}
    conn = JiraConnector("https://acme.atlassian.net", project="PROJ", page_size=2, **common(rec, env))
    items = conn.fetch_items()

    assert [i.item_key for i in items] == ["PROJ-1", "PROJ-2", "PROJ-3"]
    first, second = rec.requests
    assert first.url.path == "/rest/api/3/search/jql"
    assert params_of(first)["jql"] == "project = PROJ ORDER BY created ASC"
    assert params_of(first)["maxResults"] == "2"
    assert "nextPageToken" not in params_of(first)
    assert params_of(second)["nextPageToken"] == "abc"
    assert base64.b64decode(first.headers["Authorization"][6:]).decode() == f"bot@example.com:{FAKE_TOKEN}"

    assert items[0].state == "open"
    assert items[0].labels == "frontend,q2"
    assert items[0].created_at == "2026-06-01T09:00:00Z"
    assert items[2].state == "closed"
    assert items[2].resolution_hours == 24.0
    assert items[2].url == "https://acme.atlassian.net/browse/PROJ-3"


def test_jira_server_start_at_and_bearer():
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(params_of(request)["startAt"])
        issues = [jira_issue(1), jira_issue(2)] if start == 0 else [jira_issue(3)]
        return httpx.Response(200, json={"issues": issues, "total": 3, "startAt": start})

    rec = Recorder(handler=handler)
    conn = JiraConnector("https://jira.example.com", jql="assignee = currentUser()", flavor="server", page_size=2,
                         **common(rec, {"NEXDASH_JIRA_API_TOKEN": FAKE_TOKEN}))
    items = conn.fetch_items()
    assert len(items) == 3
    assert rec.requests[0].url.path == "/rest/api/2/search"
    assert rec.requests[0].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert params_of(rec.requests[1])["startAt"] == "2"


def test_jira_cloud_requires_email_variable():
    rec = Recorder([])
    conn = JiraConnector("https://acme.atlassian.net", project="PROJ",
                         **common(rec, {"NEXDASH_JIRA_API_TOKEN": FAKE_TOKEN}))
    with pytest.raises(ConnectorConfigError, match="NEXDASH_JIRA_EMAIL"):
        conn.fetch_items()


def test_jira_rejects_bad_project_key():
    with pytest.raises(ConnectorConfigError):
        JiraConnector("https://acme.atlassian.net", project="proj OR 1=1", env={}, resolver=public_resolver)


# ---------------------------------------------------------------------------
# Generic REST
# ---------------------------------------------------------------------------

FIELD_MAP = {
    "item_id": "id",
    "title": "name",
    "status": "state.label",
    "assignee": "owner.name",
    "labels": "tags",
    "created_at": "created",
}


def generic_task(task_id: int, state: str = "todo") -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "state": {"label": state},
        "owner": {"name": "Ivan"},
        "tags": [{"name": "ops"}],
        "created": "2026-07-01T00:00:00Z",
    }


def test_generic_page_pagination_with_custom_header():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(params_of(request)["page"])
        data = {1: [generic_task(1), generic_task(2)], 2: [generic_task(3, "Done")]}.get(page, [])
        return httpx.Response(200, json={"data": {"items": data}})

    rec = Recorder(handler=handler)
    conn = GenericRestConnector(
        "https://tasks.example.com/api", "v1/tasks", field_map=FIELD_MAP, items_path="data.items",
        pagination="page", page_size=2, auth_header="X-Api-Token", auth_scheme="", source_name="tasktool",
        query={"archived": "false"}, **common(rec, {"TASKS_TOKEN": FAKE_TOKEN}), token_env="TASKS_TOKEN",
    )
    items = conn.fetch_items()

    assert [i.item_id for i in items] == ["1", "2", "3"]
    assert len(rec.requests) == 2
    first = rec.requests[0]
    assert first.url.path == "/api/v1/tasks"
    assert first.headers["X-Api-Token"] == FAKE_TOKEN
    assert params_of(first) == {"archived": "false", "per_page": "2", "page": "1"}
    assert items[0].source == "tasktool"
    assert items[0].status == "todo"
    assert items[0].labels == "ops"
    assert items[0].assignee == "Ivan"
    assert items[2].state == "closed"


def test_generic_offset_with_total_path():
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(params_of(request)["offset"])
        data = [generic_task(offset + 1), generic_task(offset + 2)][: 3 - offset]
        return httpx.Response(200, json={"results": data, "meta": {"total": 3}})

    rec = Recorder(handler=handler)
    conn = GenericRestConnector(
        "https://tasks.example.com", "tasks", field_map=FIELD_MAP, items_path="results", pagination="offset",
        size_param="limit", total_path="meta.total", page_size=2, **common(rec, {"NEXDASH_GENERIC_TOKEN": "t"}),
    )
    assert [i.item_id for i in conn.fetch_items()] == ["1", "2", "3"]
    assert [params_of(r)["offset"] for r in rec.requests] == ["0", "2"]


def test_generic_cursor_pagination():
    rec = Recorder(
        [
            httpx.Response(200, json={"items": [generic_task(1)], "next_cursor": "c2"}),
            httpx.Response(200, json={"items": [generic_task(2)], "next_cursor": None}),
        ]
    )
    conn = GenericRestConnector(
        "https://tasks.example.com", "tasks", field_map=FIELD_MAP, items_path="items", pagination="cursor",
        **common(rec, {"NEXDASH_GENERIC_TOKEN": "t"}),
    )
    assert len(conn.fetch_items()) == 2
    assert "cursor" not in params_of(rec.requests[0])
    assert params_of(rec.requests[1])["cursor"] == "c2"


def test_generic_link_pagination_and_no_token_mode():
    rec = Recorder(
        [
            httpx.Response(200, json=[generic_task(1)],
                           headers={"Link": '<https://tasks.example.com/tasks?page=2>; rel="next"'}),
            httpx.Response(200, json=[generic_task(2)]),
        ]
    )
    conn = GenericRestConnector(
        "https://tasks.example.com", "tasks", field_map=FIELD_MAP, pagination="link", require_token=False,
        **common(rec, {}),
    )
    assert len(conn.fetch_items()) == 2
    assert "Authorization" not in rec.requests[0].headers
    assert str(rec.requests[1].url) == "https://tasks.example.com/tasks?page=2"


def test_generic_config_validation():
    kwargs = {"env": {}, "resolver": public_resolver}
    with pytest.raises(ConnectorConfigError, match="item_id"):
        GenericRestConnector("https://t.example.com", "x", field_map={"title": "name"}, **kwargs)
    with pytest.raises(ConnectorConfigError, match="unknown columns"):
        GenericRestConnector("https://t.example.com", "x", field_map={"item_id": "id", "evil": "x"}, **kwargs)
    with pytest.raises(ConnectorConfigError, match="relative"):
        GenericRestConnector("https://t.example.com", "https://other.example.net/x", field_map={"item_id": "id"},
                             **kwargs)
    with pytest.raises(ConnectorConfigError, match="pagination"):
        GenericRestConnector("https://t.example.com", "x", field_map={"item_id": "id"}, pagination="pages",
                             **kwargs)


def test_generic_items_path_must_be_list():
    rec = Recorder([httpx.Response(200, json={"data": {"items": {"not": "a list"}}})])
    conn = GenericRestConnector("https://tasks.example.com", "tasks", field_map=FIELD_MAP, items_path="data.items",
                                **common(rec, {"NEXDASH_GENERIC_TOKEN": "t"}))
    with pytest.raises(ConnectorError, match="not a list"):
        conn.fetch_items()


# ---------------------------------------------------------------------------
# Semantic layer integration
# ---------------------------------------------------------------------------


def test_example_cube_matches_builtin_definition():
    path = Path(__file__).parent.parent / "examples" / "connectors" / "work_items.yaml"
    assert load_cube_from_yaml(path) == work_items_cube()


def _sample_items() -> list[WorkItem]:
    return [
        WorkItem(source="github", item_id="1", status="open", assignee="ann", created_at="2026-01-01T00:00:00Z"),
        WorkItem(source="github", item_id="2", status="closed", assignee="ann",
                 created_at="2026-01-01T00:00:00Z", closed_at="2026-01-01T10:00:00Z"),
        WorkItem(source="jira", item_id="3", status="Done", assignee="ben",
                 created_at="2026-01-01T00:00:00Z", closed_at="2026-01-05T00:00:00Z"),
    ]


def test_sync_connector_then_query_through_cube():
    rec = Recorder(
        [
            httpx.Response(200, json=[
                gh_issue(1),
                gh_issue(2, state="closed", closed_at="2026-03-01T19:00:00Z"),
                gh_issue(3, state="closed", closed_at="2026-03-04T10:00:00Z"),
            ])
        ]
    )
    conn = GitHubConnector("acme", "widgets", **common(rec, {"NEXDASH_GITHUB_TOKEN": FAKE_TOKEN}))
    cube = work_items_cube()
    with SQLiteExecutor(":memory:") as executor:
        assert sync_connector(conn, executor) == 3
        plan = QueryPlan(
            cube="work_items",
            dimensions=["state"],
            metrics=["item_count", "max_resolution_hours"],
            order_by=[PlanOrderBy(field="state", direction="asc")],
        )
        rows = executor.execute_plan(plan, cube).rows
        assert rows == [
            {"state": "closed", "item_count": 2, "max_resolution_hours": 72.0},
            {"state": "open", "item_count": 1, "max_resolution_hours": None},
        ]

        # Refresh replaces rows instead of duplicating them.
        assert load_work_items(executor, _sample_items()) == 3
        total = executor.execute_plan(QueryPlan(cube="work_items", metrics=["item_count"]), cube).rows
        assert total == [{"item_count": 3}]


def test_load_into_sqlalchemy_executor_and_filter_with_bound_params():
    cube = work_items_cube()
    with DatabaseExecutor("sqlite:///:memory:") as executor:
        load_work_items(executor, _sample_items())
        load_work_items(executor, _sample_items())
        plan = QueryPlan(
            cube="work_items",
            dimensions=["source"],
            metrics=["item_count", "avg_resolution_hours"],
            filters=[PlanFilter(field="state", operator="eq", value="closed")],
            order_by=[PlanOrderBy(field="source", direction="asc")],
        )
        compiled = executor.compiler.compile(plan, cube, dialect=executor.dialect_name)
        assert "closed" not in compiled.sql
        assert "closed" in compiled.params.values()
        rows = executor.execute_compiled(compiled).rows
        assert rows == [
            {"source": "github", "item_count": 1, "avg_resolution_hours": 10.0},
            {"source": "jira", "item_count": 1, "avg_resolution_hours": 96.0},
        ]


def test_cube_allowlist_still_applies_to_connector_data():
    cube = work_items_cube()
    with SQLiteExecutor(":memory:") as executor:
        load_work_items(executor, _sample_items())
        with pytest.raises(AllowlistError):
            executor.execute_plan(QueryPlan(cube="work_items", dimensions=["password"]), cube)


def test_load_rejects_unsafe_table_name():
    with SQLiteExecutor(":memory:") as executor, pytest.raises(ValueError):
        load_work_items(executor, _sample_items(), table='x"; DROP TABLE y; --')
