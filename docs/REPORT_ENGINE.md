# Report engine: MCP and REST

nexdash is the dashboard layer of the portfolio. Other projects (NexCoder, NexSupport
and any future consumer) ask nexdash for a report instead of building their own: they
name a report model, pass a few validated parameters, and receive the panel data and a
dashboard they can show as is. This page is the reference for that communication and
for clients that relay it, such as NexLink.

The engine reuses what nexdash already has: the report models (`nexdash.report_models`),
their cubes on the semantic layer, the guarded SQL compiler, the connectors that load
the data, and the MCP and REST interfaces.

## Operations

| Operation | MCP tool | REST | Scope |
| --- | --- | --- | --- |
| List report models | `nexdash_list_report_models` | `GET /api/reports/models` | `reports:list` |
| Describe one model | `nexdash_describe_report_model` | `GET /api/reports/models/<name>` | `reports:list` |
| Generate a report (data and/or dashboard) | `nexdash_run_report` | `POST /api/reports/run` | `reports:run` |
| Generate only the dashboard page | | `POST /api/reports/dashboard` (answers `text/html`) | `reports:run` |

Models available today: `dev_productivity`, `security_posture` and `support_metrics`
(see [REPORT_MODELS.md](REPORT_MODELS.md)).

## Request

Both transports take the same object. Unknown fields are refused at every level.

```json
{
  "model": "support_metrics",
  "parameters": {"start": "2026-04-01", "end": "2026-06-30", "team": "tier1", "project": null},
  "output": "both",
  "locale": "en",
  "compare": true
}
```

| Field | Type | Rules |
| --- | --- | --- |
| `model` | string | Required. Lowercase identifier of an existing model. |
| `parameters.start`, `parameters.end` | `YYYY-MM-DD` or null | Inclusive. `end` must not be earlier than `start`. |
| `parameters.team`, `parameters.project` | string or null | Exact match, at most 200 characters. |
| `output` | `data`, `dashboard`, `both` | Default `both`. |
| `locale` | `en`, `pt-BR` | Number format and labels of the dashboard. Default `en`. |
| `compare` | boolean | Compare KPIs with the previous period of the same length. Default `true`. |

There is no field for SQL, query plans, table or column names. The panels of a model are
fixed `QueryPlan` objects; the parameters become bound SQL parameters and every field is
checked against the cube allowlist by the compiler. A client that needs a different
report asks for a new model; it never sends a query.

## Response

```json
{
  "engine_version": "1",
  "model": "support_metrics",
  "title": "Support metrics",
  "parameters": {"start": "2026-04-01", "end": "2026-06-30", "team": "tier1", "project": null},
  "filters": "Period: 2026-04-01 to 2026-06-30 | Team: tier1 | Project: all",
  "generated_at": "2026-07-01 12:00 UTC",
  "errors": {},
  "data": {
    "panels": {
      "volume": {"kind": "kpi", "title": "...", "unit": "count", "columns": ["ticket_count"],
                 "rows": [{"ticket_count": 100}], "value": 100, "previous_value": 96,
                 "snapshot": null, "error": null, "truncated": false}
    }
  },
  "dashboard": {
    "format": "html",
    "content_type": "text/html; charset=utf-8",
    "csp": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "locale": "en",
    "html": "<!doctype html>..."
  }
}
```

- `data` is present for `output` `data` and `both`; `dashboard` for `dashboard` and `both`.
- Panel rows are capped at 500 per panel; `truncated` says when the cap was hit.
- A panel that fails keeps the rest of the report: it appears in `errors` and shows an
  error state on the dashboard. The message is generic; the cause is logged on the
  server by exception type only.

### The dashboard

The HTML is one self-contained page: a `<style>` block and inline SVG charts, no
scripts, no external assets and no network calls. Every value from data or parameters
is HTML-escaped. Serve it with the CSP in `dashboard.csp` (the REST endpoint
`/api/reports/dashboard` already sends that header), embed it in a sandboxed iframe,
attach it to an e-mail, or save it as a file.

Over MCP the page is also attached to the tool result as an embedded resource:

```json
{"type": "resource", "resource": {"uri": "nexdash://reports/support_metrics/dashboard.html",
                                  "mimeType": "text/html", "text": "<!doctype html>..."}}
```

## Errors

| Situation | REST | MCP |
| --- | --- | --- |
| Missing or invalid token | 401 | HTTP 401 on `POST /mcp` |
| Token lacks the scope | 403 | Tool result with `isError: true` |
| Invalid request or parameters | 400 | Tool result with `isError: true` |
| Unknown model | 404 | Tool result with `isError: true` |
| Server started without report models | 503 | Report tools are not listed |
| Database failure | 500, generic message | Tool result with `isError: true` |

Error bodies are `{"error": "..."}`. Validation messages name the field and the rule
(`parameters: Value error, Parameter 'end' must not be earlier than 'start'`) and
never echo the rejected value.

## Authentication and scopes

Tokens come only from the environment or a `.env` file in the working directory, never
from code, the command line or the query string. They travel as
`Authorization: Bearer <token>` (or `X-API-Key`), are compared in constant time, and
are never logged.

- `NEXDASH_API_TOKEN` is the operator token. It holds every scope, including `admin`.
- Each consuming project gets its own token and scope list:

```bash
# .env on the nexdash server
NEXDASH_CLIENT_NEXCODER_TOKEN=<generated>
NEXDASH_CLIENT_NEXCODER_SCOPES=reports:list,reports:run
NEXDASH_CLIENT_NEXSUPPORT_TOKEN=<generated>
NEXDASH_CLIENT_NEXSUPPORT_SCOPES=reports:list,reports:run
```

Generate tokens with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Tokens shorter than 16 characters, unknown scopes, a client reusing the operator token
and a client asking for `admin` are refused at startup. A client token without a scopes
variable holds no scope.

| Scope | Allows |
| --- | --- |
| `reports:list` | List and describe report models |
| `reports:run` | Generate report data and dashboards |
| `semantic:read` | List and describe cubes (`/api/cubes`, `nexdash_list_cubes`, `nexdash_describe_cube`) |
| `semantic:query` | Structured query plans (`/api/plan`, `nexdash_query`) |
| `semantic:ask` | Natural-language questions (`/api/ask`, `nexdash_ask`) |
| `admin` | Guarded read-only SQL on `/api/query`; operator token only |

Over MCP on HTTP any valid token may connect; `tools/list` shows only the tools its
scopes allow, and each `tools/call` checks the scope again. MCP over stdio checks no
token: the client launched the process with its own privileges.

## Starting the engine

```bash
# Synthetic data, local only
nexdash --demo --report-models --host 127.0.0.1 --port 8000

# Real data: load the model tables (connectors, posture snapshots, tickets), then
nexdash --report-models --db-url "postgresql+psycopg://user@db/analytics"
```

MCP over stdio, as configured in an MCP client:

```json
{"mcpServers": {"nexdash": {"command": "nexdash", "args": ["--mcp", "--report-models", "--demo"]}}}
```

## Calling it

REST:

```bash
curl -s -H "Authorization: Bearer $NEXDASH_ENGINE_TOKEN" -H "Content-Type: application/json" \
  -d '{"model": "dev_productivity", "parameters": {"start": "2026-04-01", "end": "2026-06-30"}}' \
  http://127.0.0.1:8000/api/reports/run
```

MCP (JSON-RPC 2.0, the same messages over stdio or `POST /mcp`):

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize",
 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "nexcoder", "version": "1"}}}
{"jsonrpc": "2.0", "method": "notifications/initialized"}
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "nexdash_run_report",
            "arguments": {"model": "dev_productivity", "parameters": {"team": "platform"}, "output": "both"}}}
```

## Reproducible examples

`examples/report_engine/` holds two clients that run offline on synthetic data:

```bash
python examples/report_engine/rest_client.py --out dashboards/   # REST, local server on 127.0.0.1
python examples/report_engine/mcp_client.py                      # MCP over stdio (subprocess)
python examples/report_engine/mcp_client.py --http               # MCP over HTTP with a client token
```

Each one lists the models, generates a report, checks that the dashboard is a valid
page (doctype, one `<main>`, at least one chart, no `<script>`, expected title) and shows
that invalid parameters are refused. `rest_client.py --url <server>` talks to a running
server and reads its token from `NEXDASH_ENGINE_TOKEN`. `tests/test_report_engine.py`
runs the same clients in the test suite.
