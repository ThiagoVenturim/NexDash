# Guarding LLM-generated SQL

nexdash never runs SQL written by a language model. The model writes a `QueryPlan`; the
server writes the SQL. This page lists every check between a model answer (or a client
request) and the database, and the separate guard for the few places that accept
hand-written read-only SQL.

## Two paths to the database

| Path | Who writes the SQL | Entry points | Guard |
| --- | --- | --- | --- |
| Semantic plan | The `SQLCompiler`, from a validated plan | `POST /api/plan`, `POST /api/ask`, MCP `nexdash_query` and `nexdash_ask`, report models, live dashboard `sql` sources with a `plan` | Schema, identifiers, cube allowlist, bound parameters |
| Read-only SQL | A human operator | `POST /api/query` (scope `admin` only), live dashboard `sql` sources with `sql` | `validate_select_sql` plus bound parameters |

Model output only ever enters the first path.

## Semantic plan: layered checks

Each layer runs before the next one and stops the request with an error.

1. **Prompt boundary.** `PlanGenerator` shows the model logical names only (cubes,
   dimensions, metrics, operators, join names). Physical tables and column mappings are
   not in the prompt, so the model cannot target them. The question is fenced as data
   and limited to 2000 characters.
2. **Schema.** The answer must parse as a `QueryPlan` (Pydantic). Wrong types, unknown
   operators, a `limit` outside 1..100000, a negative `offset`, duplicate metrics or
   dimensions, or an empty plan are rejected. Filter values must be scalars (or lists
   of scalars for `in`, `not_in` and `between`), so a value can never be an object that
   expands into SQL.
3. **Identifiers.** Every name in the plan must match `^[a-zA-Z_][a-zA-Z0-9_]*$`, or
   dotted parts of that form for join fields. Quotes, spaces, semicolons and comments
   cannot appear in a name.
4. **Cube allowlist** (`SQLCompiler._validate_allowlists`). The plan's cube must be the
   cube being compiled. Every dimension, metric, join, filter field and filter operator,
   time range field and order field must be declared in that cube's YAML. A failure is an
   `AllowlistError`.
5. **SQL construction.** The compiler builds the statement with SQLAlchemy expressions
   (`sa.table`, `sa.column`, `sa.func`), never by string concatenation. Table and column
   names come from the cube definition, not from the plan.
6. **Bound parameters.** Every filter value and time range bound becomes a
   `sa.bindparam`; `in` lists use expanding parameters. The SQL text holds placeholders
   such as `:p_1_status` and the values travel separately to the driver.
7. **Service limits.** Through HTTP and MCP, `QueryService` caps `limit` at 1000 rows,
   and every endpoint needs a token with the matching scope (`semantic:read`,
   `semantic:query`, `semantic:ask`). Database and model errors are logged by type only
   and answered with a generic message.

### Reproducible example

Runs from the repository root (executed by `tests/test_docs.py`):

```python
from pydantic import ValidationError

from nexdash import AllowlistError, CubeRegistry, QueryPlan, SQLCompiler

registry = CubeRegistry.from_directory("examples/cubes")
orders = registry.get("orders")
compiler = SQLCompiler()


def check(plan_json):
    try:
        plan = QueryPlan.model_validate(plan_json)
        compiler.compile(plan, orders, registry=registry)
    except ValidationError as exc:
        return f"schema: {exc.errors()[0]['msg']}"
    except AllowlistError as exc:
        return f"allowlist: {exc}"
    return "compiled"


# An injection attempt in a name never reaches SQL: it fails the identifier check.
print(check({"cube": "orders", "metrics": ["order_count"], "dimensions": ["status; DROP TABLE fact_orders"]}))

# A real column that the cube does not expose is refused.
print(check({"cube": "orders", "metrics": ["order_count"], "dimensions": ["customer_id"]}))

# A declared field with an operator the cube does not allow is refused.
print(check({"cube": "orders", "metrics": ["order_count"],
             "filters": [{"field": "order_date", "operator": "like", "value": "2026%"}]}))

# Hostile text in a value is harmless: it is a bound parameter, not SQL.
plan = QueryPlan(cube="orders", metrics=["order_count"],
                 filters=[{"field": "status", "operator": "eq", "value": "x' OR '1'='1"}])
compiled = compiler.compile(plan, orders)
print(compiled.sql)
print(compiled.params)
assert "OR '1'='1" not in compiled.sql
assert compiled.params["p_1_status"] == "x' OR '1'='1"
```

Output:

```
schema: Value error, Invalid field 'status; DROP TABLE fact_orders': must match qualified identifier pattern
allowlist: Dimension 'customer_id' is not declared in cube 'orders'
allowlist: Filter on field 'order_date' with operator 'like' is not in allowlist for cube 'orders'
SELECT count(*) AS order_count
FROM fact_orders
WHERE fact_orders.status = :p_1_status
{'p_1_status': "x' OR '1'='1"}
```

## Read-only SQL guard

`validate_select_sql` (in `nexdash.sources`) protects the two places that accept SQL text
from an operator. It removes comments and single-quoted literals, then requires:

- the statement starts with `SELECT` or `WITH`;
- there is no `;` outside literals (one statement only);
- none of these words appear outside literals: `insert`, `update`, `delete`, `merge`,
  `replace`, `upsert`, `exec`, `execute`, `call`, `drop`, `alter`, `create`, `truncate`,
  `rename`, `grant`, `revoke`, `deny`, `backup`, `restore`, `shutdown`, `dbcc`, `into`,
  `openrowset`, `openquery`, `opendatasource`, `bulk`, `waitfor`, `attach`, `detach`,
  `pragma`, `vacuum`, and any `xp_*` or `sp_*` procedure.

Values go in `params` and are bound by the driver (`:name` placeholders). In live
dashboards, parameter names are validated and parameter values must be scalars; secrets
are referenced as `{{env.NAME}}` and substituted server-side, never written in the
recipe.

```python
from nexdash.sources import SQLGuardError, validate_select_sql

assert validate_select_sql("SELECT status, amount FROM fact_orders WHERE amount >= :min_amount")
assert validate_select_sql("SELECT ';' AS literal_semicolon")

for sql in (
    "SELECT 1; DROP TABLE fact_orders",
    "DELETE FROM fact_orders",
    "WITH x AS (DELETE FROM fact_orders RETURNING *) SELECT * FROM x",
    "SELECT * INTO backup_orders FROM fact_orders",
    "EXEC xp_cmdshell 'dir'",
):
    try:
        validate_select_sql(sql)
    except SQLGuardError as exc:
        print("blocked:", exc)
    else:
        raise AssertionError(f"guard let through: {sql}")
```

## Limits and recommended setup

The checks above are defense in depth, not a replacement for database permissions.
Known limits:

- **Join columns.** A field such as `customers.tier` is checked against the declared join
  name (`customers`), not against a column list. Any column of a joined table is
  reachable. Join only tables or views whose columns are all safe to expose, or point the
  join at a view that holds the allowed columns.
- **Keyword guard.** `validate_select_sql` is a lexical filter. It does not parse SQL, so
  it cannot see side effects hidden in functions (for example `pg_sleep`, or a
  user-defined function that writes). It also has false positives: `replace()` and
  columns named like a blocked word are refused.
- **`/api/query` has no row cap.** It is restricted to the `admin` scope for that reason.
- **Time range presets.** `TimeRange.preset` is validated as an identifier but not
  compiled; only `start` and `end` filter rows.

Recommended setup:

- Connect with a database user that has `SELECT` on the cube sources only (see
  [DATABASE.md](DATABASE.md)). Prefer views over base tables.
- Set a statement timeout on that user or role.
- Give consuming projects client tokens with the smallest scopes they need
  (`NEXDASH_CLIENT_<NAME>_SCOPES`); keep `admin` for the operator token.
- Review cube YAML like code: it is the allowlist.
