# Semantic layer

nexdash answers analytics questions through a semantic layer. A cube, written in YAML,
declares what may be queried. A `QueryPlan` says what to query. The compiler turns the
plan into parameterized SQL, and only through the cube's allowlist. Nobody, human or
model, sends SQL to the semantic endpoints.

## Semantic layer vs text-to-SQL

A text-to-SQL system asks a language model to write a SQL statement and then runs it.
nexdash asks the model to fill a small typed form instead.

| | Text-to-SQL | nexdash semantic layer |
| --- | --- | --- |
| What the model produces | A SQL string | A JSON `QueryPlan` (cube, metrics, dimensions, filters, time range, order, limit) |
| What the model sees | Table and column names, often the full schema | Logical cube names, fields and allowed operators; no physical table names |
| How the output is checked | Parsing or keyword filters on arbitrary SQL | Pydantic schema, identifier regex, then the cube allowlist |
| Where filter values go | Wherever the model wrote them, often inline | Always bound parameters |
| Joins | Any join the model invents | Only joins declared in the cube |
| Metric definitions | Re-derived by the model on every question | Defined once in YAML (`sum(amount)` is always `total_revenue`) |
| Failure mode | Wrong or dangerous SQL that may still run | A validation error before any SQL exists |

The trade-off is expressiveness. A plan cannot express window functions, subqueries,
`CASE` expressions or arbitrary arithmetic between metrics. If a question needs them,
add a view to the database and a cube over that view, so the logic is reviewed once
instead of generated per question.

## Pipeline

```
question --(nexcoder-core model tier)--> QueryPlan JSON
         --(Pydantic validation)-------> QueryPlan
         --(SQLCompiler + cube allowlist)--> SQL text + bound parameters
         --(DatabaseExecutor / SQLiteExecutor)--> rows
```

A plan can also come straight from a client (`POST /api/plan`, MCP `nexdash_query`, a
live dashboard source or a report model). It then skips the model step and goes through
the same validation and compiler.

## Cubes

A cube is one YAML file. The example cubes live in `examples/cubes/`.

```yaml
name: orders
source: fact_orders            # table or view
description: Sales orders and transaction metrics
dimensions:
  - name: order_id
    type: string
  - name: status
    type: string
  - name: order_date
    type: date
metrics:
  - name: order_count
    aggregation: count         # count(*) when no column is given
  - name: total_revenue
    aggregation: sum
    column: amount
  - name: avg_order_value
    aggregation: avg
    column: amount
joins:
  - name: customers
    target_table: dim_customers
    relationship: many_to_one
    source_key: customer_id
    target_key: id
filters:
  - name: status
    operators: [eq, neq, in]
  - name: order_date
    operators: [eq, gte, lte, between]
```

| Section | Fields | Rules |
| --- | --- | --- |
| `name`, `source` | | Plain identifiers (`^[a-zA-Z_][a-zA-Z0-9_]*$`) |
| `dimensions` | `name`, `column` (defaults to `name`), `type`, `description` | `type` is one of `string`, `number`, `integer`, `float`, `date`, `datetime`, `boolean` |
| `metrics` | `name`, `aggregation`, `column`, `description` | `aggregation` is one of `count`, `count_distinct`, `sum`, `avg`, `min`, `max`; every aggregation except `count` needs a `column` |
| `joins` | `name`, `target_table` (defaults to `name`), `relationship`, `source_key`, `target_key` | Compiled as `source.source_key = target_table.target_key` |
| `filters` | `name`, `operators` | A bare string allows every operator on that field. Without a `filters` section, every dimension is filterable with any operator |

Dimension, metric and join names must be unique within a cube, and a metric cannot share
a name with a dimension. Load cubes with `CubeRegistry.from_directory(path)` or
`load_cube_from_yaml(path_or_text)`; the server reads them from `--cubes` or
`NEXDASH_CUBES_DIR`.

## Query plan

| Field | Type | Notes |
| --- | --- | --- |
| `cube` | identifier | Must match the cube being compiled |
| `metrics` | list of names | At least one metric or one dimension is required; no duplicates |
| `dimensions` | list of names | `join.column` reaches a column of a declared join |
| `filters` | list of `{field, operator, value}` | Operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `like`, `not_like`, `between`, `is_null`, `is_not_null` |
| `time_range` | `{field, start, end}` | Inclusive bounds on a dimension; `preset` is accepted and validated but not compiled, so pass `start` and `end` |
| `order_by` | list of `{field, direction}` | `asc` or `desc`; the field must be a selected column or a declared dimension |
| `limit`, `offset` | integers | `limit` between 1 and 100000; the HTTP and MCP service caps it at 1000 |
| `joins` | list of join names | Forces a declared join even when no field uses it |

Filter values are checked by shape: `in` and `not_in` need a non-empty list of scalars,
`between` needs exactly two scalars, the null checks take no value, and every other
operator needs one scalar. A filter on a metric name becomes a `HAVING` condition.

## Reproducible example

The block below runs as is from the repository root (it is executed by
`tests/test_docs.py`). It compiles a plan, prints the SQL and its parameters, and runs it
on an in-memory SQLite database.

```python
from nexdash import CubeRegistry, PlanFilter, PlanOrderBy, QueryPlan, SQLCompiler, SQLiteExecutor, TimeRange

registry = CubeRegistry.from_directory("examples/cubes")
orders = registry.get("orders")

plan = QueryPlan(
    cube="orders",
    dimensions=["status", "customers.tier"],
    metrics=["order_count", "total_revenue"],
    filters=[PlanFilter(field="status", operator="in", value=["completed", "pending"])],
    time_range=TimeRange(field="order_date", start="2026-01-01", end="2026-12-31"),
    order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    limit=10,
)

compiled = SQLCompiler().compile(plan, orders, registry=registry, dialect="sqlite")
print(compiled.sql)
print(compiled.params)
assert "completed" not in compiled.sql  # values are parameters, never inlined

with SQLiteExecutor(":memory:") as executor:
    executor.execute_script(
        """
        CREATE TABLE fact_orders (order_id TEXT, customer_id TEXT, status TEXT, amount REAL, order_date TEXT);
        CREATE TABLE dim_customers (id TEXT, city TEXT, tier TEXT);
        INSERT INTO dim_customers VALUES ('c1', 'Porto Alegre', 'gold'), ('c2', 'Recife', 'silver');
        INSERT INTO fact_orders VALUES
            ('o1', 'c1', 'completed', 120.0, '2026-05-01'),
            ('o2', 'c2', 'completed', 250.0, '2026-05-15'),
            ('o3', 'c2', 'pending', 70.0, '2026-05-20');
        """
    )
    result = executor.execute_plan(plan, orders, registry=registry)

for row in result:
    print(row)
assert result[0] == {"status": "completed", "customers.tier": "silver", "order_count": 1, "total_revenue": 250.0}
```

The printed SQL (SQLite dialect, line breaks as SQLAlchemy renders them):

```sql
SELECT fact_orders.status AS status, dim_customers.tier AS "customers.tier",
       count(*) AS order_count, sum(fact_orders.amount) AS total_revenue
FROM fact_orders JOIN dim_customers ON fact_orders.customer_id = dim_customers.id
WHERE fact_orders.status IN (:p_1_status_1, :p_1_status_2)
  AND fact_orders.order_date >= :tr_start_2 AND fact_orders.order_date <= :tr_end_2
GROUP BY fact_orders.status, dim_customers.tier
ORDER BY sum(fact_orders.amount) DESC
LIMIT :param_1 OFFSET :param_2
```

with parameters `{'tr_start_2': '2026-01-01', 'tr_end_2': '2026-12-31', 'param_1': 10,
'param_2': 0, 'p_1_status_1': 'completed', 'p_1_status_2': 'pending'}`. Pass
`dialect="postgresql"`, `"mysql"` or `"mssql"` to get the same statement for another
database; `DatabaseExecutor` picks the dialect of its engine automatically.

## Questions in natural language

`PlanGenerator` (used by `POST /api/ask` and MCP `nexdash_ask`) builds a prompt with the
logical catalog of every loaded cube: names, descriptions, dimension types, metric
aggregations, allowed filter operators and join names. Physical table names and column
mappings stay out of the prompt. The question is wrapped as data and capped at 2000
characters. The model, picked from a `nexcoder-core` tier (`small`, `medium`, `large`),
must answer with JSON that validates as a `QueryPlan`; `call_structured_sync` retries
once by default and an answer that never validates becomes a `PlannerError`. The plan
then goes through the allowlist like any other.

Offline, the same path runs with a fake model:

```python
from nexcoder_core.llm.fake import FakeChatModel

from nexdash import CubeRegistry
from nexdash.planner import PlanGenerator

registry = CubeRegistry.from_directory("examples/cubes")
answer = '{"cube": "orders", "dimensions": ["status"], "metrics": ["order_count"]}'
plan = PlanGenerator(llm=FakeChatModel(responses=[answer])).plan("How many orders per status?", registry)
print(plan.model_dump(exclude_defaults=True))
assert plan.metrics == ["order_count"]
```

## Related pages

- [SQL_GUARDS.md](SQL_GUARDS.md): every check between a model answer and the database.
- [LIVE_DASHBOARDS.md](LIVE_DASHBOARDS.md): dashboards whose data comes from plans or guarded SQL.
- [INTERFACES.md](INTERFACES.md): MCP, REST and the web UI.
- [DATABASE.md](DATABASE.md): database URLs and drivers.
- [CONNECTORS.md](CONNECTORS.md): loading GitHub, Azure DevOps, Redmine, Jira and REST data into the `work_items` cube.
- [REPORT_MODELS.md](REPORT_MODELS.md) and [REPORT_ENGINE.md](REPORT_ENGINE.md): ready-made reports built on cubes.
