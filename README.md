# askdash

Dashboards and reports from natural-language questions, exposed over MCP and REST.

askdash answers questions through a semantic layer: cubes described in YAML, a
structured plan validated by Pydantic, and parameterized SQL compiled through an
allowlist. The language model never writes free SQL. Models can be open source
(Ollama, vLLM) or paid, selected through `faccoder-core` tiers.

Status: early development.

## Semantic Layer Architecture

The semantic layer prevents SQL injection and halluncinated queries by ensuring that
the LLM only selects cubes and outputs structured parameters validated against a strict schema:

1. **Cube Models (YAML)**: Analytical entities declaring tables/views, dimensions, metrics with aggregations, allowed joins, and allowed filters.
2. **Query Plan (Pydantic)**: Structured consultation request containing target cube, metrics, dimensions, filters, time ranges, ordering, and limits.
3. **Guarded SQL Compiler**: Compiles the query plan into parameterized SQL using SQLAlchemy constructs. Rejects any dimension, metric, join, or filter not in the allowlist. Filter values are always bound as parameters.
4. **SQLite Executor**: Executes compiled queries against SQLite and returns structured records.

### Example Usage

```python
from askdash import (
    CubeRegistry,
    PlanFilter,
    PlanOrderBy,
    QueryPlan,
    SQLiteExecutor,
    TimeRange,
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

# 3. Execute with safety allowlists and parameter binding
executor = SQLiteExecutor("orders.db")
result = executor.execute_plan(plan, orders_cube, registry=registry)

for row in result:
    print(row)
```

## Live Dashboards

Agents write self-contained HTML templates while the server collects data from
configured sources (`http`, `sql`, `csv`, `sheets`), stores secrets encrypted at rest
with Fernet (`{{env.NAME}}`), and injects data into the page as `window.ASKDASH_DATA`
under a strict Content-Security-Policy with `connect-src 'none'`:

- **SSRF Guard**: Rejects loopback, cloud metadata (`169.254.169.254`), private networks, and single-label internal hostnames, re-validating every redirect hop and stripping credentials across hosts.
- **SELECT-Only Guard**: Enforces read-only parameterized `SELECT` / `WITH ... SELECT` queries (or semantic `QueryPlan` compilation) and blocks `DELETE`, `DROP`, `EXEC`, `xp_*`, and multi-statement queries.
- **Lifecycle & Tokens**: Supports scheduled refresh cadences (`never`, `hourly`, `daily`, `weekly`) with fail-safe retention of the last good dataset, 30-day expiry with renewal tokens (`renewal_token`), and SHA-256 hashed one-time edit tokens (`ed_...`).

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

askdash grew out of analytical dashboard concepts originally explored in ApoIA and lochome,
re-architected as an independent open-source semantic query engine.

## License

MIT
