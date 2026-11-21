# Interfaces: MCP, REST and web UI

All three interfaces share one query service. A request ends up as a
`QueryPlan` that is validated, compiled through the cube allowlist into
parameterized SQL and executed. Clients and models never send free SQL to the
semantic endpoints.

## Starting the server

```bash
# Token for the REST API, the HTTP MCP endpoint and the web UI
export NEXDASH_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# Synthetic sales, support and stock cubes on in-memory SQLite
nexdash --demo --host 127.0.0.1 --port 8000

# Your own cubes and database
nexdash --cubes ./cubes --db-url "postgresql+psycopg://user@db/analytics"
```

The token can also live in a `.env` file in the working directory (see
`.env.example`). It is never accepted in the query string and never logged.
Tokens shorter than 16 characters are rejected at startup. Without a token every
protected endpoint answers 401; `NEXDASH_ALLOW_ANONYMOUS=1` turns that off for
local demos only.

| Option | Environment | Meaning |
| --- | --- | --- |
| `--host`, `--port` | `HOST`, `PORT` | Bind address (default `0.0.0.0:8000`) |
| `--db-url` | `DB_URL` | Database URL (default in-memory SQLite) |
| `--cubes DIR` | `NEXDASH_CUBES_DIR` | Directory of cube YAML files |
| `--demo` | `NEXDASH_DEMO=1` | Load the synthetic demo cubes and data (SQLite only) |
| `--report-models` | `NEXDASH_REPORT_MODELS=1` | Register the report model cubes (`dev_productivity`, `security_posture`, `support_metrics`); with `--demo`, load synthetic rows. See [REPORT_MODELS.md](REPORT_MODELS.md) |
| `--tier` | `NEXDASH_LLM_TIER` | Model tier for questions: `small`, `medium`, `large` |
| `--mcp` | | Serve MCP over stdio instead of HTTP |

## Models

Questions (`/api/ask`, `nexdash_ask`) are turned into a plan by a chat model from
`nexcoder-core`. The tier is mapped to a provider in `nexcoder.yaml`, so the same
server runs on Ollama, vLLM or a paid API by changing that file or its preset
(for example `all-open-source` for Ollama, `local-vllm`, `all-paid`). The model is created on the first
question, so starting the server never contacts a provider. The model only sees
cube names, logical fields and allowed operators; its answer must validate as a
`QueryPlan` and then pass the same allowlist as any other plan.

## REST API

Send `Authorization: Bearer <token>` (or `X-API-Key: <token>`).

| Method and path | Body | Answer |
| --- | --- | --- |
| `GET /health` | | Status, version, database (password masked). Public. |
| `GET /api/cubes` | | `{"cubes": [{name, description, dimensions, metrics}]}` |
| `GET /api/cubes/<name>` | | Dimensions, metrics and allowed filter operators |
| `POST /api/plan` | `{"plan": QueryPlan}` | `{cube, plan, sql, columns, rows, row_count}` |
| `POST /api/ask` | `{"question": "..."}` | Same as `/api/plan`, plus `question` |
| `POST /api/query` | `{"sql": "SELECT ...", "params": {}}` | Guarded read-only SQL |
| `POST /mcp` | JSON-RPC 2.0 | MCP over HTTP |
| `GET /api/reports/models` | | Report models; see [REPORT_ENGINE.md](REPORT_ENGINE.md) |
| `GET /api/reports/models/<name>` | | One report model with its request schema |
| `POST /api/reports/run` | `{"model": ..., "parameters": {...}}` | Panel data and HTML dashboard |
| `POST /api/reports/dashboard` | Same as `/api/reports/run` | The dashboard page (`text/html`) |

Each endpoint needs a scope on the token: the operator token (`NEXDASH_API_TOKEN`)
holds all of them, client tokens (`NEXDASH_CLIENT_<NAME>_TOKEN`) only those listed
in `NEXDASH_CLIENT_<NAME>_SCOPES`. A valid token without the scope gets 403. See
[REPORT_ENGINE.md](REPORT_ENGINE.md#authentication-and-scopes).

Results are capped at 1000 rows. Errors come back as `{"error": "..."}` with a
4xx or 5xx status; database and provider exceptions are reported generically.

```bash
curl -s -H "Authorization: Bearer $NEXDASH_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plan": {"cube": "sales", "dimensions": ["region"], "metrics": ["revenue"],
       "time_range": {"field": "sale_date", "start": "2026-03-01", "end": "2026-03-31"}}}' \
  http://127.0.0.1:8000/api/plan
```

## MCP

Tools:

- `nexdash_list_cubes`
- `nexdash_describe_cube` (`cube`)
- `nexdash_query` (`plan`, a `QueryPlan` object)
- `nexdash_ask` (`question`)
- `nexdash_list_report_models`, `nexdash_describe_report_model` (`model`) and
  `nexdash_run_report` (`model`, `parameters`, `output`, `locale`), with `--report-models`

Stdio transport for clients that launch the server themselves. The process runs
with the caller's privileges, so no token is checked there:

```json
{
  "mcpServers": {
    "nexdash": {
      "command": "nexdash",
      "args": ["--mcp", "--demo"]
    }
  }
}
```

Over HTTP, `POST /mcp` takes the same JSON-RPC messages behind the API token.

## Web UI

Open `http://127.0.0.1:8000/`, paste the token (kept in the tab's
`sessionStorage`), pick a cube, tick dimensions and metrics and run, or type a
question. The page is static, loads no third-party resources and is served with
`Content-Security-Policy: default-src 'none'; script-src 'self'; connect-src 'self'; ...`.

## Example cubes

`src/nexdash/demo/cubes/` holds three cubes over generated data (fixed seed,
January to June 2026):

- `sales`: sales by date, month, region, channel and product; `sale_count`,
  `units_sold`, `revenue`, `avg_ticket`.
- `support`: tickets by date, month, priority, category, status and channel;
  `ticket_count`, `avg_resolution_hours`, `max_resolution_hours`, `avg_csat`.
- `stock`: inventory per SKU and warehouse; `sku_count`, `units_on_hand`,
  `stock_value`, `avg_unit_cost`, with a `below_reorder` flag.

```python
from nexdash.demo import load_demo_registry, seed_demo_data
from nexdash.executor import DatabaseExecutor
from nexdash.service import QueryService

executor = DatabaseExecutor("sqlite:///:memory:")
seed_demo_data(executor)
service = QueryService(load_demo_registry(), executor)
print(service.run_plan({"cube": "stock", "dimensions": ["warehouse"], "metrics": ["stock_value"]})["rows"])
```
