"""Offline unit tests for live dashboard data sources, guards, and Fernet secrets."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from askdash.cube import CubeRegistry
from askdash.executor import SQLiteExecutor
from askdash.plan import PlanFilter, QueryPlan
from askdash.sources import (
    HttpFetchResult,
    SourceCollectionError,
    SourceValidationError,
    SQLGuardError,
    SSRFError,
    check_ssrf_host,
    collect_recipe_data,
    collect_single_source,
    decrypt_secrets,
    encrypt_secrets,
    extract_json_path,
    extract_secret_refs,
    fetch_url_guarded,
    google_sheets_csv_url,
    mask_secrets,
    merge_secrets,
    normalize_recipe,
    normalize_secrets,
    normalize_source,
    parse_csv_bytes,
    preview_recipe_sources,
    validate_secret_refs,
    validate_select_sql,
    validate_url,
)


def _public_resolver(host: str) -> list[str]:
    """Fake offline DNS resolver mapping external test hosts to a public documentation IP."""
    mapping = {
        "api.example.com": ["93.184.216.34"],
        "cdn.example.org": ["93.184.216.35"],
        "docs.google.com": ["142.250.190.78"],
        "files.example.com": ["93.184.216.36"],
    }
    if host in mapping:
        return mapping[host]
    raise SSRFError(f"Unmapped host in test resolver: {host}")


# ---------------------------------------------------------------------------
# SSRF Guard Tests
# ---------------------------------------------------------------------------


def test_ssrf_blocks_metadata_and_loopback_ips():
    with pytest.raises(SSRFError, match="restricted address"):
        check_ssrf_host("169.254.169.254")

    with pytest.raises(SSRFError, match="restricted address"):
        check_ssrf_host("127.0.0.1")

    with pytest.raises(SSRFError, match="restricted address"):
        check_ssrf_host("::1")

    with pytest.raises(SSRFError, match="restricted address"):
        check_ssrf_host("0.0.0.0")


def test_ssrf_blocks_single_label_and_private_hosts():
    with pytest.raises(SSRFError, match="Single-label host"):
        check_ssrf_host("localhost")

    with pytest.raises(SSRFError, match="Single-label host"):
        check_ssrf_host("postgres")

    with pytest.raises(SSRFError, match="private network"):
        check_ssrf_host("10.0.0.15")

    with pytest.raises(SSRFError, match="private network"):
        check_ssrf_host("192.168.1.20")

    with pytest.raises(SSRFError, match="private network"):
        check_ssrf_host("internal.corp.local", resolver=lambda _: ["172.16.5.4"])

    # Even when allow_private=True, metadata 169.254.169.254 and loopback remain blocked
    assert check_ssrf_host("10.0.0.15", allow_private=True) == ["10.0.0.15"]
    with pytest.raises(SSRFError):
        check_ssrf_host("169.254.169.254", allow_private=True)
    with pytest.raises(SSRFError):
        check_ssrf_host("127.0.0.1", allow_private=True)


def test_ssrf_checked_on_every_redirect_and_strips_cross_host_credentials():
    seen_Calls: list[tuple[str, dict[str, str]]] = []

    def fake_fetcher(method: str, url: str, headers: dict[str, str], body: object) -> HttpFetchResult:
        seen_Calls.append((url, dict(headers)))
        if url == "https://api.example.com/start":
            return HttpFetchResult(
                content=b"",
                status_code=302,
                headers={"Location": "https://cdn.example.org/final"},
            )
        if url == "https://cdn.example.org/final":
            return HttpFetchResult(content=b'{"ok": true}', content_type="application/json")
        raise AssertionError(f"Unexpected URL: {url}")

    res = fetch_url_guarded(
        "GET",
        "https://api.example.com/start",
        headers={"Authorization": "Bearer secret-tok", "Accept": "application/json"},
        resolver=_public_resolver,
        http_fetcher=fake_fetcher,
    )
    assert res.content == b'{"ok": true}'
    assert seen_Calls[0][1]["Authorization"] == "Bearer secret-tok"
    # Cross-host redirect must strip Authorization header
    assert "Authorization" not in seen_Calls[1][1]
    assert seen_Calls[1][1]["Accept"] == "application/json"

    # Redirecting to 169.254.169.254 must be blocked by SSRF guard
    def malicious_redirect_fetcher(
        method: str, url: str, headers: dict[str, str], body: object
    ) -> HttpFetchResult:
        return HttpFetchResult(
            content=b"",
            status_code=302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )

    with pytest.raises(SSRFError, match="169.254.169.254"):
        fetch_url_guarded(
            "GET",
            "https://api.example.com/redirect-ssrf",
            resolver=_public_resolver,
            http_fetcher=malicious_redirect_fetcher,
        )


def test_validate_url_rules():
    assert validate_url("https://api.example.com/v1/items") == "https://api.example.com/v1/items"
    with pytest.raises(SourceValidationError, match="http:// or https://"):
        validate_url("file:///etc/passwd")
    with pytest.raises(SourceValidationError, match="username or password"):
        validate_url("https://admin:hunter2@api.example.com/v1")


# ---------------------------------------------------------------------------
# SELECT-Only SQL Guard Tests
# ---------------------------------------------------------------------------


def test_select_sql_guard_allows_select_and_with():
    q1 = validate_select_sql("SELECT id, status, amount FROM orders WHERE status = :status;")
    assert q1 == "SELECT id, status, amount FROM orders WHERE status = :status"

    q2 = validate_select_sql(
        """
        -- Comment mentioning DELETE or xp_cmdshell
        WITH regional AS (
            SELECT customer_id, amount FROM orders WHERE note = 'do not delete; keep'
        )
        SELECT customer_id, SUM(amount) AS total FROM regional GROUP BY customer_id
        """
    )
    assert "WITH regional" in q2


@pytest.mark.parametrize(
    "bad_sql",
    [
        "",
        "DELETE FROM orders WHERE id = 1",
        "SELECT * FROM orders; DELETE FROM orders",
        "EXEC xp_cmdshell 'whoami'",
        "SELECT * FROM orders WHERE xp_cmdshell('a') = 1",
        "UPDATE orders SET amount = 0",
        "INSERT INTO orders VALUES (1, 2)",
        "DROP TABLE orders",
        "TRUNCATE TABLE orders",
        "SELECT * INTO new_table FROM orders",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE 'other.db' AS other",
    ],
)
def test_select_sql_guard_rejects_dangerous_queries(bad_sql: str):
    with pytest.raises(SQLGuardError):
        validate_select_sql(bad_sql)


# ---------------------------------------------------------------------------
# Fernet Secrets Tests
# ---------------------------------------------------------------------------


def test_fernet_secrets_roundtrip_and_key_isolation():
    secrets_in = {"API_TOKEN": "tok_live_987654", "DB_PASS": "s3cr3t_pass"}
    token = encrypt_secrets(secrets_in, secret_key="key-one")
    assert token != ""
    assert "tok_live_987654" not in token

    decrypted = decrypt_secrets(token, secret_key="key-one")
    assert decrypted == secrets_in

    with pytest.raises(SourceValidationError, match="cannot be decrypted"):
        decrypt_secrets(token, secret_key="wrong-key")


def test_secrets_normalization_merging_and_masking():
    base = normalize_secrets({"TOKEN": "abc12345", "OLD_KEY": "remove_me"})
    merged = merge_secrets(base, {"TOKEN": "new_secret_value", "OLD_KEY": "", "EXTRA": "xyz987"})
    assert merged == {"TOKEN": "new_secret_value", "EXTRA": "xyz987"}

    with pytest.raises(SourceValidationError, match="Invalid secret name"):
        normalize_secrets({"bad-name!": "val"})

    err_msg = "Connection failed for Bearer new_secret_value and key xyz987"
    masked = mask_secrets(err_msg, merged)
    assert "new_secret_value" not in masked
    assert "xyz987" not in masked
    assert "****" in masked


def test_credential_literals_rejected_in_sources():
    with pytest.raises(SourceValidationError, match="looks like a credential"):
        normalize_source(
            {
                "name": "sales_api",
                "kind": "http",
                "url": "https://api.example.com/sales",
                "headers": {"Authorization": "Bearer literal_secret_token"},
            }
        )

    with pytest.raises(SourceValidationError, match="looks like a credential"):
        normalize_source(
            {
                "name": "sales_db",
                "kind": "sql",
                "sql": "SELECT 1",
                "connection": {"host": "db.example.com", "password": "literal_password"},
            }
        )

    # Referencing {{env.API_TOKEN}} is accepted and tracked
    src = normalize_source(
        {
            "name": "sales_api",
            "kind": "http",
            "url": "https://api.example.com/sales",
            "headers": {"Authorization": "Bearer {{env.API_TOKEN}}"},
        }
    )
    refs = extract_secret_refs([src])
    assert refs == {"API_TOKEN"}
    with pytest.raises(SourceValidationError, match="Missing required secrets: API_TOKEN"):
        validate_secret_refs([src], {})


# ---------------------------------------------------------------------------
# Sources Collection Tests (HTTP, SQL, CSV, Sheets)
# ---------------------------------------------------------------------------


def test_collect_http_source_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer super_secret_token"
        assert request.url.params["region"] == "south"
        return httpx.Response(
            200,
            json={"data": {"items": [{"month": "2026-01", "revenue": 1500}]}},
        )

    transport = httpx.MockTransport(handler)
    source = {
        "name": "monthly_sales",
        "kind": "http",
        "method": "GET",
        "url": "https://api.example.com/v1/metrics",
        "headers": {"Authorization": "Bearer {{env.API_TOKEN}}"},
        "params": {"region": "south"},
        "json_path": "data.items",
    }
    result = collect_single_source(
        source,
        {"API_TOKEN": "super_secret_token"},
        resolver=_public_resolver,
        transport=transport,
    )
    assert result == [{"month": "2026-01", "revenue": 1500}]
    assert extract_json_path({"a": [{"b": 42}]}, "a.0.b") == 42


def test_collect_csv_source_inline_and_url():
    csv_text = "code;product;price_br;qty\n00123;Widget;1.234,50;10\n00124;Gadget;99.5;5\n"
    parsed = parse_csv_bytes(csv_text)
    assert parsed == [
        {"code": "00123", "product": "Widget", "price_br": 1234.5, "qty": 10},
        {"code": "00124", "product": "Gadget", "price_br": 99.5, "qty": 5},
    ]

    inline_res = collect_single_source(
        {"name": "inventory", "kind": "csv", "content": csv_text},
    )
    assert len(inline_res) == 2
    assert inline_res[0]["code"] == "00123"


def test_collect_sheets_source_and_private_sheet_detection():
    edit_url = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit#gid=42"
    export_url = google_sheets_csv_url(edit_url)
    assert (
        export_url
        == "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/export?format=csv&gid=42"
    )
    tab_url = google_sheets_csv_url(edit_url, sheet="Q1 Targets")
    assert (
        tab_url
        == "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/gviz/tq?tqx=out:csv&sheet=Q1%20Targets"
    )

    def sheets_fetcher(method: str, url: str, headers: dict[str, str], body: object) -> HttpFetchResult:
        assert "export?format=csv&gid=42" in url
        return HttpFetchResult(
            content=b"team,target\nCore,120\nGrowth,85\n",
            content_type="text/csv",
        )

    rows = collect_single_source(
        {"name": "targets", "kind": "sheets", "url": edit_url},
        resolver=_public_resolver,
        http_fetcher=sheets_fetcher,
    )
    assert rows == [{"team": "Core", "target": 120}, {"team": "Growth", "target": 85}]

    # Private sheet returning Google login HTML is rejected with a clear message
    def login_html_fetcher(
        method: str, url: str, headers: dict[str, str], body: object
    ) -> HttpFetchResult:
        return HttpFetchResult(
            content=b"<html><body>Sign in to Google Accounts</body></html>",
            content_type="text/html; charset=utf-8",
        )

    with pytest.raises(SourceCollectionError, match="HTML login page"):
        collect_single_source(
            {"name": "targets", "kind": "sheets", "url": edit_url},
            resolver=_public_resolver,
            http_fetcher=login_html_fetcher,
        )


def test_collect_sql_source_parameterized_and_semantic_plan():
    executor = SQLiteExecutor(":memory:")
    executor.execute_script(
        """
        CREATE TABLE fact_orders (
            order_id TEXT PRIMARY KEY,
            customer_id TEXT,
            status TEXT,
            amount REAL,
            order_date TEXT
        );
        INSERT INTO fact_orders VALUES
            ('o1', 'c1', 'completed', 120.0, '2026-05-01'),
            ('o2', 'c1', 'completed', 180.0, '2026-05-03'),
            ('o3', 'c2', 'cancelled', 70.0, '2026-05-20');
        """
    )

    # 1. Parameterized SELECT query
    sql_rows = collect_single_source(
        {
            "name": "completed_orders",
            "kind": "sql",
            "sql": "SELECT order_id, amount FROM fact_orders WHERE status = :status ORDER BY order_id",
            "params": {"status": "completed"},
        },
        sql_executor=executor,
    )
    assert sql_rows == [
        {"order_id": "o1", "amount": 120.0},
        {"order_id": "o2", "amount": 180.0},
    ]

    # 2. Semantic QueryPlan compiled via CubeRegistry
    cubes_dir = Path(__file__).parent.parent / "examples" / "cubes"
    registry = CubeRegistry.from_directory(cubes_dir)
    plan = QueryPlan(
        cube="orders",
        dimensions=["status"],
        metrics=["order_count", "total_revenue"],
        filters=[PlanFilter(field="status", operator="eq", value="completed")],
    )
    plan_rows = collect_single_source(
        {
            "name": "semantic_orders",
            "kind": "sql",
            "plan": plan.model_dump(mode="json"),
        },
        sql_executor=executor,
        cube_registry=registry,
    )
    assert plan_rows == [{"status": "completed", "order_count": 2, "total_revenue": 300.0}]

    # 3. Multi-source recipe collection and preview
    recipe = normalize_recipe(
        [
            {
                "name": "orders_summary",
                "kind": "sql",
                "plan": plan.model_dump(mode="json"),
            },
            {
                "name": "quotas",
                "kind": "csv",
                "content": "region,quota\nSouth,500\n",
            },
        ]
    )
    dataset = collect_recipe_data(recipe, sql_executor=executor, cube_registry=registry)
    assert dataset["rows"] == {"orders_summary": 1, "quotas": 1}
    assert dataset["sources"]["quotas"] == [{"region": "South", "quota": 500}]

    preview = preview_recipe_sources(recipe, sql_executor=executor, cube_registry=registry)
    assert len(preview) == 2
    assert preview[0]["ok"] is True
    assert preview[1]["sample"] == [{"region": "South", "quota": 500}]
    executor.close()
