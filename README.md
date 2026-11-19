# nexdash

Dashboards and reports from natural-language questions, exposed over MCP and REST.

*(Formerly known as askdash during early development.)*

nexdash answers questions through a semantic layer: cubes described in YAML, a
structured plan validated by Pydantic, and parameterized SQL compiled through an
allowlist. The language model never writes free SQL. Models can be open source
(Ollama, vLLM) or paid, selected through `nexcoder-core` tiers.

Status: early development.

## Semantic Layer Architecture

The semantic layer prevents SQL injection and halluncinated queries by ensuring that
the LLM only selects cubes and outputs structured parameters validated against a strict schema:

1. **Cube Models (YAML)**: Analytical entities declaring tables/views, dimensions, metrics with aggregations, allowed joins, and allowed filters.
2. **Query Plan (Pydantic)**: Structured consultation request containing target cube, metrics, dimensions, filters, time ranges, ordering, and limits.
3. **Guarded SQL Compiler**: Compiles the query plan into parameterized SQL using SQLAlchemy constructs. Rejects any dimension, metric, join, or filter not in the allowlist. Filter values are always bound as parameters.
4. **Pluggable Database Executor**: Executes compiled queries against SQLite (default) or any external database (PostgreSQL, MySQL, Microsoft SQL Server, ODBC) using SQLAlchemy engines.
5. **Managed Reports (`ReportStore`)**: Full lifecycle for analytical reports (`create_draft`, `preview`, `diff`, `publish`, `rollback`, `discard_draft`) governed by single-use, short-lived human approval codes.

### Example Usage

```python
from nexdash import (
    CubeRegistry,
    DatabaseExecutor,
    PlanFilter,
    PlanOrderBy,
    QueryPlan,
    TimeRange,
    get_executor,
)

# 1. Load cubes
registry = CubeRegistry.from_directory("examples/cubes")
orders_cube = registry.get("orders")

# 2. Build structured plan
plan = QueryPlan(
    cube="orders",
    dimensions=["status"],
    metrics=["order_count", "total_revenue"],
    filters=[
        PlanFilter(field="status", operator="eq", value="completed"),
    ],
    time_range=TimeRange(field="order_date", start="2026-01-01", end="2026-12-31"),
    order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    limit=10,
)

# 3. Execute with safety allowlists and parameter binding (SQLite default or via DB_URL)
executor = get_executor()  # or DatabaseExecutor("postgresql+psycopg://...")
result = executor.execute_plan(plan, orders_cube, registry=registry)

for row in result:
    print(row)
```

## Pluggable Database & Containers

NexDash runs zero-config on SQLite and seamlessly adapts to external databases:

- **Pluggable Database**: Configure `DB_URL` (e.g. `postgresql+psycopg://...`, `mysql+pymysql://...`, or `mssql+pyodbc://...`). See [docs/DATABASE.md](docs/DATABASE.md) for full instructions and driver extras.
- **Containers**: Multi-arch `Dockerfile` and `docker-compose.yml` (with app and optional PostgreSQL).
- **Kubernetes**: Production manifests located in `k8s/` (`deployment.yaml`, `service.yaml`, `configmap.yaml`, `secret.yaml`).
- **Cross-Platform Scripts**: `scripts/run.ps1` for Windows PowerShell and `scripts/run.sh` for Linux/macOS.

## Live Dashboards

Agents write self-contained HTML templates while the server collects data from
configured sources (`http`, `sql`, `csv`, `sheets`), stores secrets encrypted at rest
with Fernet (`{{env.NAME}}`), and injects data into the page as `window.NEXDASH_DATA`
under a strict Content-Security-Policy with `connect-src 'none'`:

- **SSRF Guard**: Rejects loopback, cloud metadata (`169.254.169.254`), private networks, and single-label internal hostnames, re-validating every redirect hop and stripping credentials across hosts.
- **SELECT-Only Guard**: Enforces read-only parameterized `SELECT` / `WITH ... SELECT` queries (or semantic `QueryPlan` compilation) and blocks `DELETE`, `DROP`, `EXEC`, `xp_*`, and multi-statement queries.
- **Lifecycle & Tokens**: Supports scheduled refresh cadences (`never`, `hourly`, `daily`, `weekly`) with fail-safe retention of the last good dataset, 30-day expiry with renewal tokens (`renewal_token`), and SHA-256 hashed one-time edit tokens (`ed_...`).

## Data Source Connectors

`nexdash.connectors` pulls work items from GitHub, Azure DevOps, Redmine, Jira, or any
JSON REST API, normalizes them into one `work_items` table, and lets the semantic layer
query it through the `work_items` cube. Credentials come only from environment variables
or `.env`; requests stay on the configured host; rate limits are retried; GET responses
are cached. See [docs/CONNECTORS.md](docs/CONNECTORS.md).

```python
from nexdash import QueryPlan, SQLiteExecutor
from nexdash.connectors import GitHubConnector, sync_connector, work_items_cube

with GitHubConnector("owner", "repo") as github, SQLiteExecutor("nexdash.db") as executor:
    sync_connector(github, executor)  # reads NEXDASH_GITHUB_TOKEN
    plan = QueryPlan(cube="work_items", dimensions=["state"], metrics=["item_count"])
    print(executor.execute_plan(plan, work_items_cube()).rows)
```

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ../fabrica-code/packages/core -e .[dev]
git config core.hooksPath .githooks
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check .
python scripts/check_no_emoji.py $(git ls-files)
```

## Origin

nexdash grew out of analytical dashboard concepts originally explored in ApoIA and lochome,
re-architected as an independent open-source semantic query engine.

## License

MIT
