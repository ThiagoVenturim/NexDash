# Live dashboards

A live dashboard is an HTML page written by an agent (or a person) plus a data recipe
kept on the server. The server collects the data, injects it into the page as
`window.NEXDASH_DATA` and serves the result under a Content-Security-Policy that blocks
every network request from the browser. The page never holds credentials and never
calls an API itself.

The API lives in `nexdash.dashboards` (`DashboardStore`) and `nexdash.sources`. It is a
library API: the store keeps dashboards in memory, and the host application decides how
to expose publish, view, refresh, renew and edit over HTTP and how to persist them.

## Static snapshot or live

| | `publish_snapshot` | `publish_live` |
| --- | --- | --- |
| Data | Baked into the HTML by the author | Collected by the server from 1 to 5 sources |
| Refresh | None | Manual (`refresh_dashboard`) or scheduled (`cadence`) |
| Secrets | None | Fernet-encrypted at rest, referenced as `{{env.NAME}}` |
| Upgrade | `convert_to_live` keeps the tokens and adds a recipe | |

## Sources

Each source has a `name` (lowercase, becomes `window.NEXDASH_DATA.<name>`) and a `kind`.

| Kind | Required fields | Notes |
| --- | --- | --- |
| `sql` with `plan` | `plan` (a `QueryPlan`) | Compiled through the cube allowlist, like `/api/plan`. Needs `cube_registry` on the store |
| `sql` with `sql` | `sql`, optional `params` | Must pass `validate_select_sql`; values bound as parameters; see [SQL_GUARDS.md](SQL_GUARDS.md) |
| `http` | `url`, optional `method` (`GET`, `POST`), `headers`, `params`, `body`, `json_path` | SSRF guard on every redirect hop |
| `csv` | `url` or inline `content`, optional `header_row` | Numbers in `1234.5` and `1.234,5` formats are parsed |
| `sheets` | Google Sheets `url`, optional `sheet` | Fetched as CSV export through the same SSRF guard |

`max_rows` defaults to 5000 per source and is capped at 20000. The collected dataset is
capped at 8 MB, the HTML template at 8 MB, and each HTTP response at 8 MB.

Collection is all-or-nothing: if one source fails, publication fails, and a refresh
keeps the last good dataset, records the error (secrets masked) and increments
`consecutive_failures`.

SQL sources run on the store's `sql_executor` (a `DatabaseExecutor`, `SQLiteExecutor`,
SQLAlchemy engine or `sqlite3` connection). Without one, a source may name a
`connection` (`url`, `dsn` or SQLite `database` path, with passwords only as
`{{env.NAME}}`), and as a last resort `DB_URL` from the environment is used.

## Secrets

Credentials are never written in a recipe. A recipe references them as
`{{env.NAME}}` and the caller passes the values separately in `secrets`:

```python
sources = [{
    "name": "issues",
    "kind": "http",
    "url": "https://api.example.com/v1/issues",
    "headers": {"Authorization": "Bearer {{env.ISSUES_API_TOKEN}}"},
    "json_path": "items",
}]
secrets = {"ISSUES_API_TOKEN": "<your token>"}
```

- A header, query parameter or connection password whose name looks sensitive
  (`authorization`, `cookie`, `token`, `key`, `secret`, `password`, ...) is refused unless
  its value is a `{{env.NAME}}` placeholder. Credentials inside a URL
  (`https://user:pass@host`) are refused.
- Every placeholder must have a matching secret, at most 20 per dashboard.
- Secrets are encrypted with Fernet. The key is derived with HKDF-SHA256 from the store's
  `secret_key`, or from `NEXDASH_SECRET_KEY` in the environment. Set one of them in any
  shared deployment: without both, a built-in development key is used.
- Error messages are passed through `mask_secrets` before they are stored or returned.
- Public metadata lists `secret_names` only, never values.

## SSRF guard

`http`, `csv` and `sheets` sources go through `fetch_url_guarded`, which:

- accepts only `http` and `https` URLs with a hostname;
- refuses single-label hosts (`localhost`, `db`, `intranet`) and resolves every other
  hostname, refusing loopback, link-local (including `169.254.169.254`), multicast,
  unspecified and reserved addresses, and private networks unless the store was created
  with `allow_private_ssrf=True`;
- follows at most 5 redirects by hand and repeats the check on each hop;
- drops sensitive headers when a redirect changes host;
- stops reading a response after 8 MB, with a 20 second timeout.

## Rendering and the browser sandbox

`render_by_access_token(access_token)` returns the HTML and the headers to send:

```
Content-Security-Policy: default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
  img-src 'self' data: blob:; font-src 'self' data:; connect-src 'none';
  frame-ancestors 'none'; base-uri 'none'; form-action 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

The same policy is also injected as a `<meta>` tag. `connect-src 'none'` blocks `fetch`,
XHR and WebSocket calls, so the page can only show what the server injected.
`window.NEXDASH_DATA` and `window.NEXDASH_META` (updated time, refreshable flag, last
error) are written right after `<head>`, before any script of the author. The JSON is
escaped for a `<script>` block (`<`, `>`, `&`, U+2028, U+2029), so a value such as
`</script>` in the data cannot break out. A small status bar with the data time and a
Refresh button (a plain form `POST`) is added before `</body>` on live dashboards.

## Lifecycle and tokens

```
publish -> active --(3 days left: warn_d3)--(1 day left: warn_d0)--> expired --(+60 days)--> removed
             ^                                                          |
             +------------------- renew (+30 days) <--------------------+
permanent dashboards never expire; they emit warn_idle once after 90 days without a view.
```

| Token | Use | Storage |
| --- | --- | --- |
| `access_token` | View the page | Opaque random value |
| `renewal_token` | Extend validity by 30 days (capped at 60 days ahead) | Opaque random value |
| `edit_token` (`ed_...`) | Change title, HTML, sources, secrets, cadence, notes | Returned once at publication; only its SHA-256 hash is kept; compared in constant time; replaced by `rotate_edit_token` |

Cadences are `never`, `hourly` (next full hour), `daily` and `weekly` (Mondays), at
`refresh_time` (`HH:MM`, default `06:30`, UTC-3). The host calls two jobs on a timer:
`process_scheduled_refreshes()` and `process_expirations()`. Manual refreshes with
`enforce_cooldown=True` are limited to one per 15 minutes. Editing sources or secrets
collects data again at once.

## Reproducible example

Runs offline from the repository root (executed by `tests/test_docs.py`). It publishes a
live dashboard with a semantic plan source, a guarded SQL source and an inline CSV
source, renders it and refreshes it.

```python
from nexdash import CubeRegistry, DashboardStore, DashboardValidationError, SQLiteExecutor

executor = SQLiteExecutor(":memory:")
executor.execute_script(
    """
    CREATE TABLE fact_orders (order_id TEXT, customer_id TEXT, status TEXT, amount REAL, order_date TEXT);
    INSERT INTO fact_orders VALUES
        ('o1', 'c1', 'completed', 120.0, '2026-05-01'),
        ('o2', 'c2', 'completed', 250.0, '2026-05-15'),
        ('o3', 'c2', 'cancelled', 70.0, '2026-05-20');
    """
)
store = DashboardStore(
    secret_key="<replace with a long random value>",
    sql_executor=executor,
    cube_registry=CubeRegistry.from_directory("examples/cubes"),
)

template = """<!doctype html><html><head><title>Orders</title></head><body>
<pre id="out"></pre>
<script>document.getElementById("out").textContent = JSON.stringify(window.NEXDASH_DATA.by_status);</script>
</body></html>"""

published = store.publish_live(
    title="Orders by status",
    html=template,
    owner_email="owner@example.com",
    cadence="daily",
    sources=[
        {"name": "by_status", "kind": "sql",
         "plan": {"cube": "orders", "dimensions": ["status"], "metrics": ["order_count", "total_revenue"]}},
        {"name": "large_orders", "kind": "sql",
         "sql": "SELECT order_id, amount FROM fact_orders WHERE amount >= :min_amount",
         "params": {"min_amount": 100}},
        {"name": "targets", "kind": "csv", "content": "status,target\ncompleted,400\n"},
    ],
)
print(published["rows"])  # {'by_status': 2, 'large_orders': 2, 'targets': 1}

page = store.render_by_access_token(published["access_token"])
assert "connect-src 'none'" in page.headers["Content-Security-Policy"]
assert "window.NEXDASH_DATA=" in page.html

executor.execute_script("INSERT INTO fact_orders VALUES ('o4', 'c1', 'completed', 300.0, '2026-05-25');")
print(store.refresh_dashboard(published["id"])["rows"])  # large_orders is now 3

# A write statement in a recipe is refused before anything runs.
try:
    store.publish_live(title="Bad", html=template, owner_email="owner@example.com",
                       sources=[{"name": "bad", "kind": "sql", "sql": "DELETE FROM fact_orders"}])
except DashboardValidationError as exc:
    print("refused:", exc)

# So is a literal credential in a sensitive header.
try:
    store.publish_live(title="Bad", html=template, owner_email="owner@example.com",
                       sources=[{"name": "api", "kind": "http", "url": "https://api.example.com/v1",
                                 "headers": {"Authorization": "Bearer abc123"}}])
except DashboardValidationError as exc:
    print("refused:", exc)
```

## Related pages

- [SEMANTIC_LAYER.md](SEMANTIC_LAYER.md): cubes and query plans used by `sql` sources.
- [SQL_GUARDS.md](SQL_GUARDS.md): the read-only SQL guard and its limits.
- [REPORT_MODELS.md](REPORT_MODELS.md): ready-made, script-free dashboards built from cubes.
