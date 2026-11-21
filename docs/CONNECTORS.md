# Data Source Connectors

Connectors read project management and development tools and load their items into
the database, where the semantic layer queries them through the `work_items` cube.
The language model still only fills a `QueryPlan`; the guarded compiler turns it into
parameterized SQL. Connectors never run model-written queries.

```
tool API --(connector: auth, pagination, rate limit)--> WorkItem rows
        --(load_work_items)--> work_items table
        --(QueryPlan + work_items cube + SQLCompiler)--> results
```

## Available connectors

| kind | Class | Credential variables | Pagination |
| --- | --- | --- | --- |
| `github` | `GitHubConnector(owner, repo)` | `NEXDASH_GITHUB_TOKEN` (Bearer) | `Link: rel="next"` |
| `azure_devops` | `AzureDevOpsConnector(project, organization=...)` | `NEXDASH_AZURE_DEVOPS_TOKEN` (PAT, Basic) | WIQL ids, then batches of up to 200 |
| `redmine` | `RedmineConnector(base_url, project=None)` | `NEXDASH_REDMINE_API_KEY` (`X-Redmine-API-Key`) | `offset` / `limit` / `total_count` |
| `jira` | `JiraConnector(base_url, project=... or jql=..., flavor="cloud")` | Cloud: `NEXDASH_JIRA_EMAIL` + `NEXDASH_JIRA_API_TOKEN` (Basic). Server/Data Center: `NEXDASH_JIRA_API_TOKEN` (Bearer) | Cloud: `nextPageToken`. Server: `startAt` / `total` |
| `generic` | `GenericRestConnector(base_url, path, field_map=...)` | `NEXDASH_GENERIC_TOKEN` or any `token_env` | `none`, `page`, `offset`, `cursor`, `link` |

Every connector accepts `token_env=` to read a different variable. There is no
parameter for a literal token, and `build_connector` rejects `token`, `api_key`,
`password` and `secret` keys in configuration.

## Example `.env`

```
NEXDASH_GITHUB_TOKEN=
NEXDASH_AZURE_DEVOPS_TOKEN=
NEXDASH_REDMINE_API_KEY=
NEXDASH_JIRA_EMAIL=
NEXDASH_JIRA_API_TOKEN=
```

`.env` is ignored by git. Fill in the values locally or inject them as environment
variables (for example from the Kubernetes secret in `k8s/secret.yaml`).

## Normalized model

Each connector yields `WorkItem` objects with these columns:

`source`, `project`, `item_id`, `item_key`, `title`, `item_type`, `status`, `state`
(`open` or `closed`), `assignee`, `author`, `priority`, `labels` (sorted, comma-separated),
`created_at`, `updated_at`, `closed_at` (ISO 8601 UTC), `resolution_hours` (hours from
creation to close), `url`.

The matching cube is returned by `work_items_cube()` and is also in
[examples/connectors/work_items.yaml](../examples/connectors/work_items.yaml).
It exposes `item_count`, `assignee_count`, `avg_resolution_hours` and
`max_resolution_hours`.

## Loading and querying

```python
from nexdash import DatabaseExecutor, PlanFilter, QueryPlan
from nexdash.connectors import JiraConnector, sync_connector, work_items_cube

jira = JiraConnector("https://your-site.atlassian.net", project="PROJ")
with DatabaseExecutor() as executor:  # DB_URL or SQLite
    sync_connector(jira, executor)    # replaces the work_items table
    plan = QueryPlan(
        cube="work_items",
        dimensions=["assignee"],
        metrics=["item_count", "avg_resolution_hours"],
        filters=[PlanFilter(field="state", operator="eq", value="closed")],
    )
    rows = executor.execute_plan(plan, work_items_cube()).rows
```

Use `table=` in `sync_connector` / `load_work_items` and `work_items_cube(source=...)`
to keep one table per tool. Table names go through the same identifier allowlist as cubes.

## Generic REST connector

```python
from nexdash.connectors import GenericRestConnector

tasks = GenericRestConnector(
    "https://tasks.example.com/api",
    "v1/tasks",
    items_path="data.items",
    pagination="page",          # page/per_page query parameters
    token_env="TASKS_TOKEN",
    auth_header="X-Api-Token",
    auth_scheme="",             # send the raw token
    source_name="tasktool",
    field_map={
        "item_id": "id",
        "title": "name",
        "status": "state.label",
        "assignee": "owner.name",
        "labels": "tags",
        "created_at": "created",
    },
    closed_values=["done", "archived"],
)
```

Paths in `field_map` use dot syntax (`owner.name`, `items.0.id`). Options per
pagination style: `page_param`, `size_param`, `start_page`, `offset_param`,
`cursor_param`, `cursor_path` and `total_path`.

## Safety and limits

- **Same origin only**: pagination links that point to another host are refused and
  redirects are not followed, so credentials are never sent elsewhere.
- **SSRF guard**: the base host is checked with `check_ssrf_host`. Loopback, link-local
  and cloud metadata addresses are always blocked. Private networks are allowed by
  default (`allow_private=True`) because Redmine and Jira are often self-hosted.
- **Rate limits**: HTTP 429, 503, and 403 with `X-RateLimit-Remaining: 0` are retried
  after `Retry-After` or `X-RateLimit-Reset`, up to `max_retries` (default 3). A wait
  longer than `max_wait_s` (default 60 s) raises `ConnectorRateLimitError` with
  `retry_after_s`.
- **Cache**: GET responses are cached in memory for `cache_ttl_s` (default 60 s).
- **Page budget**: reading stops after `max_pages` (default 100) and sets
  `connector.truncated = True`.
- **No secrets in logs or errors**: log lines contain only the method and URL path, and
  messages go through `mask_secrets`. Errors are `ConnectorConfigError`,
  `ConnectorAuthError`, `ConnectorHTTPError`, `ConnectorRateLimitError`, all subclasses
  of `ConnectorError`.

## Writing a connector

Subclass `BaseConnector`, set `kind`, implement `auth_headers()` and `iter_items()`
(use `self.get_json` and `self._check_page_budget`), then call
`register_connector(MyConnector)` so `build_connector("my_kind", ...)` can create it.
Tests should use `httpx.MockTransport` through the `transport=` argument and a fake
`resolver=`, as in `tests/test_connectors.py`.
