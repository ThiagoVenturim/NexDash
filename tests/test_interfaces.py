"""Offline tests for the MCP server, the REST API, the web UI and the demo cubes.

Everything runs on synthetic data in an in-memory SQLite database. The HTTP
tests bind a server to 127.0.0.1 on a free port; no external network is used.
Model calls go through nexcoder-core's FakeChatModel.
"""

import io
import json
import logging
import threading

import httpx
import pytest
from nexcoder_core.llm.fake import FakeChatModel

from nexdash.auth import AuthConfigError, TokenAuth, extract_token
from nexdash.demo import DEMO_CUBES, generate_demo_data, load_demo_registry, seed_demo_data
from nexdash.executor import DatabaseExecutor, SQLiteExecutor
from nexdash.mcp_server import LATEST_PROTOCOL_VERSION, MCPServer
from nexdash.planner import PlanGenerator, PlannerError, build_prompt
from nexdash.server import UI_CSP, build_service, main, run_server
from nexdash.service import NotFoundError, QueryService, ServiceError

TOKEN = "unit-test-token-0123456789abcdef"

SALES_PLAN = {
    "cube": "sales",
    "dimensions": ["region"],
    "metrics": ["revenue", "sale_count"],
    "order_by": [{"field": "region", "direction": "asc"}],
}


def fake_planner(*responses: str) -> PlanGenerator:
    return PlanGenerator(llm=FakeChatModel(responses=list(responses)))


@pytest.fixture
def executor():
    ex = DatabaseExecutor("sqlite:///:memory:")
    yield ex
    ex.close()


@pytest.fixture
def service(executor) -> QueryService:
    planner = fake_planner(json.dumps({"cube": "support", "dimensions": ["priority"], "metrics": ["ticket_count"]}))
    return build_service(executor, demo=True, planner=planner)


# Demo cubes and data


def test_demo_registry_has_sales_support_stock():
    registry = load_demo_registry()
    assert tuple(registry.list_cubes()) == DEMO_CUBES


def test_demo_data_is_deterministic_and_synthetic():
    first = generate_demo_data(seed=11)
    second = generate_demo_data(seed=11)
    assert first == second
    assert generate_demo_data(seed=12) != first
    assert set(first) == {"demo_sales", "demo_tickets", "demo_stock"}
    assert all(row["sale_date"].startswith("2026-") for row in first["demo_sales"])
    unresolved = [row for row in first["demo_tickets"] if row["status"] != "resolved"]
    assert unresolved and all(row["resolution_hours"] is None for row in unresolved)


def test_seed_works_on_sqlalchemy_and_sqlite_executors():
    with DatabaseExecutor("sqlite:///:memory:") as db_ex:
        counts = seed_demo_data(db_ex)
        # Seeding twice replaces the tables instead of duplicating rows.
        assert seed_demo_data(db_ex) == counts
        total = db_ex.execute_sql("SELECT count(*) AS n FROM demo_sales").rows[0]["n"]
        assert total == counts["demo_sales"]

    with SQLiteExecutor(":memory:") as lite:
        counts = seed_demo_data(lite)
        cur = lite.connection.execute("SELECT count(*) FROM demo_stock")
        assert cur.fetchone()[0] == counts["demo_stock"] == 24


def test_every_demo_cube_answers_a_plan(service):
    plans = [
        {"cube": "sales", "dimensions": ["channel"], "metrics": ["units_sold", "avg_ticket"]},
        {"cube": "support", "dimensions": ["status"], "metrics": ["ticket_count", "avg_csat"]},
        {"cube": "stock", "dimensions": ["warehouse"], "metrics": ["units_on_hand", "stock_value"]},
    ]
    for plan in plans:
        result = service.run_plan(plan)
        assert result["row_count"] > 0
        assert result["columns"] == plan["dimensions"] + plan["metrics"]


def test_demo_refuses_non_sqlite_backends():
    class FakeExecutor:
        dialect_name = "postgresql"

    with pytest.raises(ValueError, match="SQLite"):
        build_service(FakeExecutor(), demo=True)  # type: ignore[arg-type]


# Auth


def test_token_auth_verifies_in_constant_time_and_hides_token():
    auth = TokenAuth(TOKEN)
    assert auth.configured
    assert auth.verify(TOKEN)
    assert not auth.verify(TOKEN + "x")
    assert not auth.verify(None)
    assert TOKEN not in repr(auth)


def test_token_auth_rejects_short_tokens():
    with pytest.raises(AuthConfigError):
        TokenAuth("short")


def test_missing_token_denies_unless_anonymous_is_explicit():
    assert not TokenAuth(None).verify(None)
    assert TokenAuth(None, allow_anonymous=True).verify(None)
    # A configured token always wins over the anonymous flag.
    assert not TokenAuth(TOKEN, allow_anonymous=True).verify(None)


def test_extract_token_from_headers():
    assert extract_token({"Authorization": f"Bearer {TOKEN}"}) == TOKEN
    assert extract_token({"authorization": f"bearer {TOKEN}"}) == TOKEN
    assert extract_token({"X-API-Key": TOKEN}) == TOKEN
    assert extract_token({"Authorization": "Basic abc"}) is None
    assert extract_token({}) is None


def test_token_auth_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXDASH_API_TOKEN", raising=False)
    monkeypatch.delenv("NEXDASH_ALLOW_ANONYMOUS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"NEXDASH_API_TOKEN={TOKEN}\n", encoding="utf-8")
    auth = TokenAuth.from_env(env_file)
    assert auth.verify(TOKEN)
    monkeypatch.delenv("NEXDASH_API_TOKEN", raising=False)


# Service and planner


def test_describe_cube_hides_physical_names(service):
    described = service.describe_cube("sales")
    text = json.dumps(described)
    assert "demo_sales" not in text
    assert "amount" not in text
    assert {"field": "region", "operators": ["eq", "neq", "in", "not_in"]} in described["filters"]
    with pytest.raises(NotFoundError):
        service.describe_cube("missing")


def test_run_plan_caps_rows_and_rejects_fields_outside_allowlist(service):
    service.max_rows = 3
    result = service.run_plan({"cube": "sales", "dimensions": ["sale_date"], "metrics": ["revenue"]})
    assert result["row_count"] == 3
    assert result["plan"]["limit"] == 3

    with pytest.raises(ServiceError, match="not declared"):
        service.run_plan({"cube": "sales", "metrics": ["password"]})
    with pytest.raises(ServiceError, match="Invalid plan"):
        service.run_plan({"cube": "sales; DROP TABLE demo_sales", "metrics": ["revenue"]})
    with pytest.raises(NotFoundError):
        service.run_plan({"cube": "nope", "metrics": ["x"]})
    with pytest.raises(ServiceError):
        service.run_plan("not an object")


def test_database_errors_are_not_leaked(service, executor, caplog):
    with executor.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE demo_sales")
    with caplog.at_level(logging.ERROR), pytest.raises(ServiceError) as info:
        service.run_plan(SALES_PLAN)
    assert info.value.status == 500
    assert "demo_sales" not in str(info.value)
    assert "OperationalError" in caplog.text


def test_ask_turns_question_into_plan(service):
    result = service.ask("How many tickets per priority?")
    assert result["cube"] == "support"
    assert result["question"] == "How many tickets per priority?"
    assert sum(row["ticket_count"] for row in result["rows"]) == 180


def test_ask_reports_bad_model_output(executor):
    service = build_service(executor, demo=True, planner=fake_planner("not json", "still not json"))
    with pytest.raises(ServiceError) as info:
        service.ask("revenue?")
    assert info.value.status == 422

    unknown = build_service(executor, demo=True, planner=fake_planner(json.dumps({"cube": "hr", "metrics": ["x"]})))
    with pytest.raises(ServiceError, match="unknown cube"):
        unknown.ask("salaries?")


def test_ask_hides_provider_errors(executor):
    service = build_service(executor, demo=True, planner=fake_planner(RuntimeError("key=abc at host")))  # type: ignore[arg-type]
    with pytest.raises(ServiceError) as info:
        service.ask("revenue?")
    assert info.value.status == 502
    assert "abc" not in str(info.value)


def test_ask_disabled_without_planner(executor):
    service = QueryService(load_demo_registry(), executor)
    with pytest.raises(ServiceError) as info:
        service.ask("anything")
    assert info.value.status == 501


def test_planner_prompt_and_validation():
    registry = load_demo_registry()
    prompt = build_prompt("Revenue by region", registry)
    assert "sales" in prompt and "support" in prompt and "stock" in prompt
    assert "demo_sales" not in prompt
    planner = fake_planner(json.dumps(SALES_PLAN))
    with pytest.raises(PlannerError):
        planner.plan("   ", registry)
    with pytest.raises(PlannerError):
        planner.plan("x" * 2001, registry)
    assert planner.plan("Revenue by region", registry).cube == "sales"
    with pytest.raises(ValueError):
        PlanGenerator(tier="huge")


def test_planner_retries_with_validation_error():
    model = FakeChatModel(responses=['{"cube": "sales"}', json.dumps(SALES_PLAN)])
    plan = PlanGenerator(llm=model).plan("Revenue by region", load_demo_registry())
    assert plan.metrics == ["revenue", "sale_count"]
    assert model.calls == 2


# MCP


def rpc(method, params=None, req_id=1):
    message = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def tool_payload(reply):
    return json.loads(reply["result"]["content"][0]["text"])


def test_mcp_initialize_and_list_tools(service):
    mcp = MCPServer(service)
    init = mcp.handle(rpc("initialize", {"protocolVersion": "2024-11-05"}))
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    unknown_version = mcp.handle(rpc("initialize", {"protocolVersion": "1999-01-01"}))
    assert unknown_version["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp.handle(rpc("ping"))["result"] == {}

    tools = mcp.handle(rpc("tools/list"))["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == ["nexdash_list_cubes", "nexdash_describe_cube", "nexdash_query", "nexdash_ask"]
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


def test_mcp_tools_call(service):
    mcp = MCPServer(service)
    cubes = tool_payload(mcp.handle(rpc("tools/call", {"name": "nexdash_list_cubes"})))
    assert [cube["name"] for cube in cubes["cubes"]] == ["sales", "stock", "support"]

    describe_call = {"name": "nexdash_describe_cube", "arguments": {"cube": "stock"}}
    described = tool_payload(mcp.handle(rpc("tools/call", describe_call)))
    assert "stock_value" in [metric["name"] for metric in described["metrics"]]

    reply = mcp.handle(rpc("tools/call", {"name": "nexdash_query", "arguments": {"plan": SALES_PLAN}}))
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["columns"] == ["region", "revenue", "sale_count"]

    asked = tool_payload(mcp.handle(rpc("tools/call", {"name": "nexdash_ask", "arguments": {"question": "tickets?"}})))
    assert asked["cube"] == "support"


def test_mcp_errors(service):
    mcp = MCPServer(service)
    bad_plan = mcp.handle(
        rpc("tools/call", {"name": "nexdash_query", "arguments": {"plan": {"cube": "sales", "metrics": ["secret"]}}})
    )
    assert bad_plan["result"]["isError"] is True
    assert "not declared" in tool_payload(bad_plan)["error"]

    assert mcp.handle(rpc("tools/call", {"name": "drop_tables"}))["error"]["code"] == -32602
    assert mcp.handle(rpc("tools/call", {"name": "nexdash_query", "arguments": []}))["error"]["code"] == -32602
    assert mcp.handle(rpc("resources/list"))["error"]["code"] == -32601
    assert mcp.handle({"id": 3, "method": "ping"})["error"]["code"] == -32600
    assert mcp.handle([])["error"]["code"] == -32600

    batch = mcp.handle([rpc("ping", req_id=1), {"jsonrpc": "2.0", "method": "notifications/x"}, rpc("ping", req_id=2)])
    assert [item["id"] for item in batch] == [1, 2]


def test_mcp_stdio_round_trip(service):
    lines = [
        json.dumps(rpc("initialize", {"protocolVersion": LATEST_PROTOCOL_VERSION})),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
        "{not json",
        json.dumps(rpc("tools/call", {"name": "nexdash_query", "arguments": {"plan": SALES_PLAN}}, req_id=2)),
    ]
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    MCPServer(service).serve_stdio(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(replies) == 3
    assert replies[0]["result"]["serverInfo"]["name"] == "nexdash"
    assert replies[1]["error"]["code"] == -32700
    assert replies[2]["id"] == 2 and replies[2]["result"]["isError"] is False


def test_cli_mcp_stdio(monkeypatch, capsys):
    message = json.dumps(rpc("tools/call", {"name": "nexdash_list_cubes"}))
    monkeypatch.setattr("sys.stdin", io.StringIO(message + "\n"))
    main(["--mcp", "--demo", "--db-url", "sqlite:///:memory:"])
    reply = json.loads(capsys.readouterr().out.strip())
    assert len(tool_payload(reply)["cubes"]) == 3


# REST API and web UI over a real local socket


@pytest.fixture
def http_server():
    planner = fake_planner(json.dumps(SALES_PLAN))
    server = run_server(
        "127.0.0.1",
        0,
        "sqlite:///:memory:",
        demo=True,
        auth=TokenAuth(TOKEN),
        planner=planner,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
        yield server, client
    server.shutdown()
    server.server_close()
    server.executor.close()


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_rest_health_is_public(http_server):
    _, client = http_server
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_rest_requires_token(http_server):
    _, client = http_server
    for method, path in (("GET", "/api/cubes"), ("POST", "/api/plan"), ("POST", "/api/ask"), ("POST", "/mcp")):
        response = client.request(method, path, json={})
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"].startswith("Bearer")
    assert client.get("/api/cubes", headers={"Authorization": "Bearer wrong-token-0123456789"}).status_code == 401
    # Tokens in the query string are ignored on purpose.
    assert client.get(f"/api/cubes?token={TOKEN}").status_code == 401
    assert client.get("/api/cubes", headers={"X-API-Key": TOKEN}).status_code == 200


def test_rest_cubes_plan_and_ask(http_server):
    _, client = http_server
    cubes = client.get("/api/cubes", headers=AUTH).json()["cubes"]
    assert [cube["name"] for cube in cubes] == ["sales", "stock", "support"]

    described = client.get("/api/cubes/support", headers=AUTH)
    assert described.status_code == 200
    assert "avg_csat" in [metric["name"] for metric in described.json()["metrics"]]
    assert client.get("/api/cubes/missing", headers=AUTH).status_code == 404

    result = client.post("/api/plan", json={"plan": SALES_PLAN}, headers=AUTH)
    assert result.status_code == 200
    body = result.json()
    assert [row["region"] for row in body["rows"]] == ["east", "north", "south", "west"]
    assert body["sql"].startswith("SELECT")

    bad = client.post("/api/plan", json={"plan": {"cube": "sales", "metrics": ["nope"]}}, headers=AUTH)
    assert bad.status_code == 400
    assert "not declared" in bad.json()["error"]

    asked = client.post("/api/ask", json={"question": "Revenue by region"}, headers=AUTH)
    assert asked.status_code == 200
    assert asked.json()["rows"] == body["rows"]


def test_rest_mcp_endpoint(http_server):
    _, client = http_server
    reply = client.post("/mcp", json=rpc("tools/list"), headers=AUTH)
    assert reply.status_code == 200
    assert len(reply.json()["result"]["tools"]) == 4
    notification = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AUTH)
    assert notification.status_code == 202


def test_rest_raw_sql_stays_read_only(http_server):
    _, client = http_server
    ok = client.post("/api/query", json={"sql": "SELECT count(*) AS n FROM demo_stock"}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["rows"] == [{"n": 24}]
    blocked = client.post("/api/query", json={"sql": "DROP TABLE demo_stock"}, headers=AUTH)
    assert blocked.status_code == 400


def test_rest_rejects_bad_bodies(http_server):
    _, client = http_server
    headers = {**AUTH, "Content-Type": "application/json"}
    assert client.post("/api/plan", content=b"{not json", headers=headers).status_code == 400
    assert client.post("/api/plan", content=b"", headers=headers).status_code == 400
    assert client.post("/api/plan", content=b"[]", headers=headers).status_code == 400
    too_big = client.post("/api/plan", content=b"x" * (1024 * 1024 + 1), headers=headers)
    assert too_big.status_code == 413
    assert client.get("/nowhere").status_code == 404


def test_web_ui_is_served_with_strict_csp(http_server):
    _, client = http_server
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == UI_CSP
    assert "connect-src 'self'" in UI_CSP and "'unsafe-inline'" not in UI_CSP
    assert '<script src="/ui/app.js"' in page.text
    script = client.get("/ui/app.js")
    assert script.status_code == 200 and "textContent" in script.text and "innerHTML" not in script.text
    assert client.get("/ui/style.css").headers["content-type"].startswith("text/css")


def test_server_without_token_explains_and_never_logs_secrets(caplog):
    with caplog.at_level(logging.INFO):
        server = run_server("127.0.0.1", 0, "sqlite:///:memory:", demo=True, auth=TokenAuth(None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
            response = client.get("/api/cubes", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 401
        assert "NEXDASH_API_TOKEN" in response.json()["error"]
    finally:
        server.shutdown()
        server.server_close()
        server.executor.close()
    assert "NEXDASH_API_TOKEN is not set" in caplog.text
    assert TOKEN not in caplog.text
