"""Offline tests for the shared report engine over MCP and REST (issue #11).

Synthetic data only, in-memory SQLite. HTTP tests bind to 127.0.0.1 on a free port;
the MCP stdio test launches ``python -m nexdash --mcp`` as a subprocess. No external
network and no real credentials are used.
"""

import datetime as dt
import json
import logging
import sys
import threading
from pathlib import Path

import httpx
import pytest

from nexdash.auth import (
    ALL_SCOPES,
    SCOPE_REPORTS_LIST,
    SCOPE_REPORTS_RUN,
    AuthConfigError,
    Principal,
    TokenAuth,
    parse_scopes,
)
from nexdash.engine import PANEL_ERROR_MESSAGE, REPORT_DASHBOARD_CSP, EngineError, ReportEngine, RunRequest
from nexdash.executor import DatabaseExecutor
from nexdash.mcp_server import TOOL_SCOPES, MCPServer
from nexdash.report_models import REPORT_MODELS
from nexdash.report_models.synthetic import seed_model_data
from nexdash.server import build_service, run_server
from nexdash.service import NotFoundError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "report_engine"
sys.path.insert(0, str(EXAMPLES))

from engine_example_support import check_dashboard  # noqa: E402

OPERATOR = "operator-token-0123456789abcdef"
CLIENT = "client-token-reports-0123456789"
LIST_ONLY = "client-token-list-only-01234567"
FIXED_NOW = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)

REQUEST = {
    "model": "security_posture",
    "parameters": {"start": "2026-04-01", "end": "2026-06-30", "team": "apps"},
}

INVALID_REQUESTS = [
    ({"model": "security_posture", "parameters": {"start": "2026-06-30", "end": "2026-01-01"}}, 400),
    ({"model": "security_posture", "parameters": {"start": "not-a-date"}}, 400),
    ({"model": "security_posture", "parameters": {"sql": "SELECT 1"}}, 400),
    ({"model": "security_posture", "parameters": {"team": "x" * 201}}, 400),
    ({"model": "security_posture", "plan": {"cube": "security_posture", "metrics": ["x"]}}, 400),
    ({"model": "security_posture", "output": "pdf"}, 400),
    ({"model": "security_posture", "locale": "fr"}, 400),
    ({"model": "Robert'); DROP TABLE x;--"}, 400),
    ({"model": "no_such_model"}, 404),
    ({}, 400),
]


@pytest.fixture
def executor():
    ex = DatabaseExecutor("sqlite:///:memory:")
    seed_model_data(ex)
    yield ex
    ex.close()


@pytest.fixture
def engine(executor) -> ReportEngine:
    return ReportEngine(executor, clock=lambda: FIXED_NOW)


# Engine


def test_list_models_covers_every_report_model(engine):
    models = engine.list_models()
    assert [m["name"] for m in models] == sorted(REPORT_MODELS)
    for model in models:
        assert model["parameters"] == ["start", "end", "team", "project"]
        assert model["outputs"] == ["data", "dashboard", "both"]
        assert model["panel_ids"]


def test_describe_model_has_panels_and_schemas(engine):
    info = engine.describe_model("support_metrics")
    assert {p["id"] for p in info["panels"]} == set(info["panel_ids"])
    assert set(info["parameters_schema"]["properties"]) == {"start", "end", "team", "project"}
    assert info["request_schema"]["additionalProperties"] is False
    with pytest.raises(NotFoundError):
        engine.describe_model("missing")


def test_run_returns_data_and_valid_dashboard(engine):
    payload = engine.run({**REQUEST, "output": "both"})
    assert payload["model"] == "security_posture"
    assert payload["parameters"] == {"start": "2026-04-01", "end": "2026-06-30", "team": "apps", "project": None}
    assert payload["generated_at"] == "2026-07-01 12:00 UTC"
    assert payload["errors"] == {}
    panels = payload["data"]["panels"]
    assert set(panels) == {p.id for p in REPORT_MODELS["security_posture"].panels}
    assert any(p["kind"] == "kpi" and p["value"] is not None for p in panels.values())
    dashboard = payload["dashboard"]
    assert dashboard["csp"] == REPORT_DASHBOARD_CSP
    assert check_dashboard(dashboard["html"], title=payload["title"]) == []
    assert "Team: apps" in dashboard["html"]
    json.dumps(payload)  # JSON-friendly


def test_run_output_selection_and_locale(engine):
    data_only = engine.run({**REQUEST, "output": "data"})
    assert "data" in data_only and "dashboard" not in data_only
    page_only = engine.run({**REQUEST, "output": "dashboard", "locale": "pt-BR"})
    assert "data" not in page_only
    assert '<html lang="pt-BR">' in page_only["dashboard"]["html"]
    assert engine.render(REQUEST).startswith("<!doctype html>")


def test_run_matches_direct_model_run(engine, executor):
    payload = engine.run({**REQUEST, "output": "data"})
    direct = REPORT_MODELS["security_posture"].run(executor, REQUEST["parameters"])
    for panel_id, entry in direct.dataset().items():
        assert payload["data"]["panels"][panel_id]["rows"] == entry["rows"]


@pytest.mark.parametrize(("request_body", "status"), INVALID_REQUESTS)
def test_invalid_requests_are_refused(engine, request_body, status):
    with pytest.raises(EngineError if status == 400 else NotFoundError) as info:
        engine.run(request_body)
    assert info.value.status == status


def test_non_object_request_is_refused(engine):
    for body in (None, "security_posture", ["security_posture"], 3):
        with pytest.raises(EngineError):
            engine.run(body)


def test_validation_messages_do_not_echo_values(engine):
    with pytest.raises(EngineError) as info:
        engine.run({"model": "security_posture", "parameters": {"start": "secret-value-xyz"}})
    assert "secret-value-xyz" not in str(info.value)


def test_panel_errors_are_generic(caplog):
    # Tables were never created: every panel fails, and no SQL or driver text leaves the server.
    with DatabaseExecutor("sqlite:///:memory:") as empty:
        payload = ReportEngine(empty).run({"model": "dev_productivity"})
    assert payload["errors"]
    assert set(payload["errors"].values()) == {PANEL_ERROR_MESSAGE}
    text = json.dumps(payload)
    assert "no such table" not in text.lower()
    assert "select" not in json.dumps(payload["data"]).lower()


def test_rows_are_capped(executor):
    payload = ReportEngine(executor, max_panel_rows=1).run({"model": "dev_productivity", "output": "data"})
    panels = payload["data"]["panels"]
    assert all(len(p["rows"]) <= 1 for p in panels.values())
    assert any(p["truncated"] for p in panels.values())


def test_run_request_model_forbids_extra_fields():
    with pytest.raises(ValueError):
        RunRequest.model_validate({"model": "dev_productivity", "sql": "SELECT 1"})


# Scoped tokens


def test_client_tokens_carry_their_scopes():
    auth = TokenAuth(OPERATOR, clients={"nexcoder": (CLIENT, "reports:list,reports:run"), "viewer": (LIST_ONLY, "")})
    assert auth.identify(OPERATOR) == Principal("operator", ALL_SCOPES)
    client = auth.identify(CLIENT)
    assert client is not None and client.name == "nexcoder"
    assert client.scopes == {SCOPE_REPORTS_LIST, SCOPE_REPORTS_RUN}
    assert auth.identify(LIST_ONLY).scopes == frozenset()
    assert auth.identify("wrong-token-0123456789abcdef") is None
    assert auth.identify(None) is None
    assert CLIENT not in repr(auth) and OPERATOR not in repr(auth)


def test_client_only_auth_disables_anonymous():
    auth = TokenAuth(None, allow_anonymous=True, clients={"support": (CLIENT, "reports:run")})
    assert auth.configured
    assert not auth.allow_anonymous
    assert auth.identify(None) is None


def test_scope_parsing_rejects_unknown_and_reserved():
    assert parse_scopes(" reports:list , reports:run ") == {SCOPE_REPORTS_LIST, SCOPE_REPORTS_RUN}
    with pytest.raises(AuthConfigError):
        parse_scopes("reports:write")
    with pytest.raises(AuthConfigError):
        parse_scopes("admin")


def test_client_token_rules():
    with pytest.raises(AuthConfigError):
        TokenAuth(None, clients={"short": ("short", "reports:list")})
    with pytest.raises(AuthConfigError):
        TokenAuth(OPERATOR, clients={"copy": (OPERATOR, "reports:list")})
    with pytest.raises(AuthConfigError):
        TokenAuth(None, clients={"operator": (CLIENT, "reports:list")})


def test_client_tokens_from_env_and_dotenv(tmp_path, monkeypatch):
    for key in ("NEXDASH_API_TOKEN", "NEXDASH_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NEXDASH_CLIENT_NEXCODER_TOKEN", CLIENT)
    monkeypatch.setenv("NEXDASH_CLIENT_NEXCODER_SCOPES", "reports:list,reports:run")
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"NEXDASH_CLIENT_SUPPORT_TOKEN={LIST_ONLY}\nNEXDASH_CLIENT_SUPPORT_SCOPES=reports:list\n", encoding="utf-8"
    )
    try:
        auth = TokenAuth.from_env(env_file)
        assert sorted(auth.client_names) == ["nexcoder", "support"]
        assert auth.identify(CLIENT).scopes == {SCOPE_REPORTS_LIST, SCOPE_REPORTS_RUN}
        assert auth.identify(LIST_ONLY).scopes == {SCOPE_REPORTS_LIST}
    finally:
        monkeypatch.delenv("NEXDASH_CLIENT_SUPPORT_TOKEN", raising=False)
        monkeypatch.delenv("NEXDASH_CLIENT_SUPPORT_SCOPES", raising=False)


# MCP (in process)


@pytest.fixture
def mcp(executor, engine) -> MCPServer:
    return MCPServer(build_service(executor), engine)


def _call(mcp, name, arguments, principal=None, req_id=1):
    reply = mcp.handle(
        {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        principal,
    )
    return reply["result"]


def test_mcp_lists_report_tools_with_scopes(mcp):
    names = [t["name"] for t in mcp.tools]
    assert {"nexdash_list_report_models", "nexdash_describe_report_model", "nexdash_run_report"} <= set(names)
    assert set(names) == set(TOOL_SCOPES)
    reports_only = Principal("support", frozenset({SCOPE_REPORTS_LIST, SCOPE_REPORTS_RUN}))
    visible = [t["name"] for t in mcp.tools_for(reports_only)]
    assert visible == ["nexdash_list_report_models", "nexdash_describe_report_model", "nexdash_run_report"]
    run_tool = next(t for t in mcp.tools if t["name"] == "nexdash_run_report")
    assert run_tool["inputSchema"]["properties"]["model"]["enum"] == sorted(REPORT_MODELS)


def test_mcp_run_report_returns_dashboard_resource(mcp):
    result = _call(mcp, "nexdash_run_report", {**REQUEST, "output": "both"})
    assert result["isError"] is False
    payload = result["structuredContent"]
    resource = next(item["resource"] for item in result["content"] if item["type"] == "resource")
    assert resource["mimeType"] == "text/html"
    assert resource["uri"] == "nexdash://reports/security_posture/dashboard.html"
    assert resource["text"] == payload["dashboard"]["html"]
    assert check_dashboard(resource["text"], title=payload["title"]) == []


def test_mcp_refuses_invalid_parameters(mcp):
    for body, _status in INVALID_REQUESTS:
        result = _call(mcp, "nexdash_run_report", body)
        assert result["isError"] is True
        assert "error" in json.loads(result["content"][0]["text"])


def test_mcp_enforces_tool_scopes(mcp):
    list_only = Principal("viewer", frozenset({SCOPE_REPORTS_LIST}))
    denied = _call(mcp, "nexdash_run_report", REQUEST, list_only)
    assert denied["isError"] is True and "reports:run" in denied["content"][0]["text"]
    allowed = _call(mcp, "nexdash_list_report_models", {}, list_only)
    assert allowed["isError"] is False
    query = _call(mcp, "nexdash_query", {"plan": {"cube": "security_posture", "metrics": ["x"]}}, list_only)
    assert query["isError"] is True and "semantic:query" in query["content"][0]["text"]


def test_mcp_without_engine_hides_report_tools(executor):
    plain = MCPServer(build_service(executor))
    assert "nexdash_run_report" not in [t["name"] for t in plain.tools]
    reply = plain.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nexdash_run_report", "arguments": {}}}
    )
    assert reply["error"]["code"] == -32602


# REST and MCP over a real local socket


@pytest.fixture
def http_server():
    auth = TokenAuth(
        OPERATOR,
        clients={"nexcoder": (CLIENT, "reports:list,reports:run"), "viewer": (LIST_ONLY, "reports:list")},
    )
    server = run_server("127.0.0.1", 0, "sqlite:///:memory:", demo=True, report_models=True, auth=auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        yield server, client
    server.shutdown()
    server.server_close()
    server.executor.close()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_rest_client_receives_a_valid_dashboard(http_server):
    _, client = http_server
    models = client.get("/api/reports/models", headers=bearer(CLIENT))
    assert models.status_code == 200
    assert [m["name"] for m in models.json()["models"]] == sorted(REPORT_MODELS)
    described = client.get("/api/reports/models/dev_productivity", headers=bearer(CLIENT))
    assert described.status_code == 200 and described.json()["name"] == "dev_productivity"

    run = client.post("/api/reports/run", json=REQUEST, headers=bearer(CLIENT))
    assert run.status_code == 200
    payload = run.json()
    assert payload["errors"] == {}
    assert check_dashboard(payload["dashboard"]["html"], title=payload["title"]) == []

    page = client.post("/api/reports/dashboard", json=REQUEST, headers=bearer(CLIENT))
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == REPORT_DASHBOARD_CSP
    assert check_dashboard(page.text, title=payload["title"]) == []


@pytest.mark.parametrize(("request_body", "status"), INVALID_REQUESTS)
def test_rest_refuses_invalid_parameters(http_server, request_body, status):
    _, client = http_server
    for path in ("/api/reports/run", "/api/reports/dashboard"):
        response = client.post(path, json=request_body, headers=bearer(CLIENT))
        assert response.status_code == status
        assert "error" in response.json()


def test_rest_refuses_non_object_bodies(http_server):
    _, client = http_server
    response = client.post("/api/reports/run", json=["security_posture"], headers=bearer(CLIENT))
    assert response.status_code == 400


def test_rest_scopes_per_operation(http_server):
    _, client = http_server
    # No token or a wrong one: 401.
    assert client.get("/api/reports/models").status_code == 401
    assert client.post("/api/reports/run", json=REQUEST, headers=bearer("x" * 32)).status_code == 401
    # List-only token cannot run reports.
    assert client.get("/api/reports/models", headers=bearer(LIST_ONLY)).status_code == 200
    assert client.post("/api/reports/run", json=REQUEST, headers=bearer(LIST_ONLY)).status_code == 403
    assert client.post("/api/reports/dashboard", json=REQUEST, headers=bearer(LIST_ONLY)).status_code == 403
    # Report clients reach neither the semantic layer nor free SQL.
    assert client.get("/api/cubes", headers=bearer(CLIENT)).status_code == 403
    plan = {"plan": {"cube": "security_posture", "metrics": ["open_findings"]}}
    assert client.post("/api/plan", json=plan, headers=bearer(CLIENT)).status_code == 403
    sql = client.post("/api/query", json={"sql": "SELECT 1"}, headers=bearer(CLIENT))
    assert sql.status_code == 403
    # The operator token keeps every scope.
    assert client.get("/api/cubes", headers=bearer(OPERATOR)).status_code == 200
    assert client.post("/api/reports/run", json=REQUEST, headers=bearer(OPERATOR)).status_code == 200


def test_mcp_over_http_with_client_token(http_server):
    _, client = http_server

    def rpc(method, params=None, token=CLIENT, req_id=1):
        body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        response = client.post("/mcp", json=body, headers=bearer(token))
        assert response.status_code == 200
        return response.json()

    tools = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    assert tools == ["nexdash_list_report_models", "nexdash_describe_report_model", "nexdash_run_report"]
    result = rpc("tools/call", {"name": "nexdash_run_report", "arguments": REQUEST})["result"]
    assert result["isError"] is False
    resource = next(item["resource"] for item in result["content"] if item["type"] == "resource")
    assert check_dashboard(resource["text"], title=result["structuredContent"]["title"]) == []

    bad = rpc("tools/call", {"name": "nexdash_run_report", "arguments": {"model": "x", "parameters": {"q": 1}}})
    assert bad["result"]["isError"] is True
    denied = rpc("tools/call", {"name": "nexdash_run_report", "arguments": REQUEST}, token=LIST_ONLY)
    assert denied["result"]["isError"] is True
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_engine_disabled_answers_503():
    server = run_server("127.0.0.1", 0, "sqlite:///:memory:", auth=TokenAuth(OPERATOR))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            assert client.get("/api/reports/models", headers=bearer(OPERATOR)).status_code == 503
            assert client.post("/api/reports/run", json=REQUEST, headers=bearer(OPERATOR)).status_code == 503
    finally:
        server.shutdown()
        server.server_close()
        server.executor.close()


def test_tokens_never_reach_logs(http_server, caplog):
    _, client = http_server
    with caplog.at_level(logging.DEBUG):
        client.post("/api/reports/run", json=REQUEST, headers=bearer(CLIENT))
        client.post("/api/reports/run", json={"model": "x"}, headers=bearer("y" * 32))
        client.get("/api/cubes", headers={"X-API-Key": LIST_ONLY})
    for token in (CLIENT, LIST_ONLY, OPERATOR, "y" * 32):
        assert token not in caplog.text


# Reproducible examples


def test_rest_example_end_to_end():
    import rest_client
    from engine_example_support import local_server

    with local_server() as server:
        result = rest_client.call_engine(server.url, server.token)
    assert result["models"] == sorted(REPORT_MODELS)
    assert result["dashboard_problems"] == []
    assert check_dashboard(result["dashboard_html"]) == []
    assert result["dashboard_csp"] == REPORT_DASHBOARD_CSP
    assert result["rejected"] == {"invalid_period": 400, "unknown_parameter": 400}


def test_mcp_example_over_http():
    import mcp_client

    result = mcp_client.run_http()
    assert result["report_is_error"] is False
    assert result["dashboard_problems"] == []
    assert result["invalid_is_error"] is True


def test_mcp_example_over_stdio():
    import mcp_client

    result = mcp_client.run_stdio()
    assert result["server"]["name"] == "nexdash"
    assert "nexdash_run_report" in result["tools"]
    assert result["report"]["model"] == "dev_productivity"
    assert result["dashboard_problems"] == []
    assert result["invalid_is_error"] is True
    assert "200 characters" in result["invalid_message"]


def test_example_main_writes_dashboard(tmp_path, capsys):
    import rest_client

    assert rest_client.main(["--out", str(tmp_path)]) == 0
    written = tmp_path / "support_metrics.html"
    assert check_dashboard(written.read_text(encoding="utf-8"), title="Support metrics") == []
    assert "Refused" in capsys.readouterr().out
