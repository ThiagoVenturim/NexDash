"""HTTP server: REST API, MCP over HTTP and a minimal web UI.

Uses only the Python standard library (``http.server``). Endpoints:

Public:
- ``GET /health``, ``GET /api/health``: liveness and database backend (password masked).
- ``GET /``, ``GET /ui/...``: static web UI (no data; it calls the API with the token).

Protected by the API token (``Authorization: Bearer`` or ``X-API-Key``):
- ``GET /api/cubes``: list cubes.
- ``GET /api/cubes/<name>``: describe one cube.
- ``POST /api/plan``: run ``{"plan": QueryPlan}`` through the allowlist compiler.
- ``POST /api/ask``: ``{"question": "..."}`` answered by the configured model tier.
- ``POST /api/query``: read-only parameterized ``SELECT`` (guarded).
- ``POST /mcp``: MCP JSON-RPC 2.0 messages.

``nexdash --mcp`` serves the MCP tools over stdio instead of starting HTTP.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from nexdash import __version__
from nexdash.auth import ALLOW_ANONYMOUS_ENV, TOKEN_ENV, TokenAuth
from nexdash.cube import CubeRegistry, load_cubes
from nexdash.db import get_db_url, sanitize_db_url
from nexdash.executor import DatabaseExecutor, SQLiteExecutor, get_executor
from nexdash.mcp_server import MCPServer
from nexdash.planner import DEFAULT_TIER, TIERS, PlanGenerator
from nexdash.service import QueryService, ServiceError
from nexdash.sources import validate_select_sql

logger = logging.getLogger("nexdash.server")

MAX_BODY_BYTES = 1024 * 1024
MAX_DRAIN_BYTES = 8 * MAX_BODY_BYTES

UI_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'"

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/ui": ("index.html", "text/html; charset=utf-8"),
    "/ui/": ("index.html", "text/html; charset=utf-8"),
    "/ui/index.html": ("index.html", "text/html; charset=utf-8"),
    "/ui/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/ui/style.css": ("style.css", "text/css; charset=utf-8"),
}


def _read_static(name: str) -> bytes:
    return resources.files("nexdash").joinpath("web", name).read_bytes()


class NexDashRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health, REST, MCP and the web UI."""

    server_version = f"NexDash/{__version__}"

    # Routing

    def do_GET(self) -> None:  # noqa: N802
        path = self._route_path()
        if path in ("/health", "/api/health"):
            self._handle_health()
        elif path in _STATIC_FILES:
            self._send_static(*_STATIC_FILES[path])
        elif path == "/api/cubes":
            if self._authorize():
                self._with_service(lambda service: {"cubes": service.list_cubes()})
        elif path.startswith("/api/cubes/"):
            if self._authorize():
                name = unquote(path[len("/api/cubes/") :])
                self._with_service(lambda service: service.describe_cube(name))
        else:
            self._send_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = self._route_path()
        if path not in ("/api/query", "/query", "/api/plan", "/api/ask", "/mcp"):
            self._send_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not self._authorize():
            return
        data = self._read_json_body()
        if data is None:
            return
        if path in ("/api/query", "/query"):
            self._handle_query(data)
        elif path == "/api/plan":
            self._with_service(lambda service: service.run_plan(_field(data, "plan")))
        elif path == "/api/ask":
            self._with_service(lambda service: service.ask(_field(data, "question")))
        else:
            self._handle_mcp(data)

    # Handlers

    def _handle_health(self) -> None:
        db_url = get_db_url()
        executor = getattr(self.server, "executor", None)
        dialect = executor.dialect_name if executor and hasattr(executor, "dialect_name") else "unknown"
        if isinstance(executor, SQLiteExecutor):
            dialect = "sqlite"
        payload = {
            "status": "ok",
            "version": __version__,
            "database": sanitize_db_url(db_url),
            "dialect": dialect,
        }
        self._send_json(payload)

    def _handle_query(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._send_json({"error": "The request body must be a JSON object"}, status=HTTPStatus.BAD_REQUEST)
            return
        sql = data.get("sql")
        params = data.get("params") or {}
        if not sql:
            self._send_json({"error": "Missing 'sql' in query payload"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(params, dict):
            self._send_json({"error": "'params' must be an object"}, status=HTTPStatus.BAD_REQUEST)
            return
        executor = getattr(self.server, "executor", None)
        if executor is None or not hasattr(executor, "execute_sql"):
            self._send_json({"error": "No database executor available"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            validated_sql = validate_select_sql(sql)
            result = executor.execute_sql(validated_sql, params)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"columns": result.columns, "rows": result.rows, "row_count": result.row_count})

    def _handle_mcp(self, data: Any) -> None:
        mcp = getattr(self.server, "mcp", None)
        if mcp is None:
            service = getattr(self.server, "service", None)
            if service is None:
                self._send_json({"error": "No cubes are configured"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                return
            mcp = MCPServer(service)
        reply = mcp.handle(data)
        if reply is None:
            self._send_empty(HTTPStatus.ACCEPTED)
            return
        self._send_json(reply)

    def _with_service(self, action: Any) -> None:
        service: QueryService | None = getattr(self.server, "service", None)
        if service is None:
            self._send_json({"error": "No cubes are configured"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            payload = action(service)
        except ServiceError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus(exc.status))
            return
        except Exception as exc:
            logger.error("Request failed: %s", type(exc).__name__)
            self._send_json({"error": "Internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    # Helpers

    def _route_path(self) -> str:
        return self.path.split("?", 1)[0].split("#", 1)[0]

    def _authorize(self) -> bool:
        auth: TokenAuth | None = getattr(self.server, "auth", None)
        if auth is not None and auth.verify_headers(self.headers):
            return True
        if auth is None or not auth.configured:
            message = f"API token not configured on the server; set {TOKEN_ENV}"
        else:
            message = "Unauthorized: missing or invalid API token"
        self._send_json(
            {"error": message},
            status=HTTPStatus.UNAUTHORIZED,
            extra_headers={"WWW-Authenticate": 'Bearer realm="nexdash"'},
        )
        return False

    def _read_json_body(self) -> Any | None:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = -1
        if length <= 0:
            self._send_json({"error": "Empty request body"}, status=HTTPStatus.BAD_REQUEST)
            return None
        if length > MAX_BODY_BYTES:
            self._discard_body(length)
            self.close_connection = True
            self._send_json({"error": "Request body too large"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            self._send_json({"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
            return None

    def _discard_body(self, length: int) -> None:
        # Read (a bounded amount of) the rejected body so the client sees the 413 instead of a reset.
        remaining = min(length, MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send_static(self, name: str, content_type: str) -> None:
        raw = _read_static(name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Security-Policy", UI_CSP)
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(
        self,
        data: Any,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Security-Policy", API_CSP)
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._send_common_headers()
        self.end_headers()

    def _send_common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def log_message(self, format: str, *args: Any) -> None:
        # The request line holds method and path only; headers (and so tokens) are never logged.
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)


def _field(data: Any, name: str) -> Any:
    if not isinstance(data, dict):
        raise ServiceError("The request body must be a JSON object.")
    return data.get(name)


def build_service(
    executor: DatabaseExecutor | SQLiteExecutor,
    *,
    cubes_dir: str | Path | None = None,
    demo: bool = False,
    planner: PlanGenerator | None = None,
    tier: str = DEFAULT_TIER,
) -> QueryService:
    """Assemble the registry (demo cubes and/or a cube directory) and the query service."""
    registry = CubeRegistry()
    if demo:
        from nexdash.demo import load_demo_registry, seed_demo_data

        dialect = getattr(executor, "dialect_name", "sqlite")
        if not isinstance(executor, SQLiteExecutor) and dialect != "sqlite":
            raise ValueError("Demo data is only loaded into SQLite; unset DB_URL or point it to a SQLite file.")
        demo_registry = load_demo_registry()
        for name in demo_registry.list_cubes():
            registry.register(demo_registry.get(name))
        counts = seed_demo_data(executor)
        logger.info("Loaded synthetic demo data: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    if cubes_dir:
        for cube in load_cubes(cubes_dir).values():
            if cube.name in registry:
                raise ValueError(f"Cube '{cube.name}' is defined twice")
            registry.register(cube)
    return QueryService(registry, executor, planner=planner or PlanGenerator(tier=tier))


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def run_server(
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8000,
    db_url: str | None = None,
    *,
    cubes_dir: str | Path | None = None,
    demo: bool = False,
    auth: TokenAuth | None = None,
    planner: PlanGenerator | None = None,
    tier: str = DEFAULT_TIER,
) -> ThreadingHTTPServer:
    """Create the NexDash HTTP server (call ``serve_forever`` to start it)."""
    _configure_logging()

    resolved_db_url = db_url or get_db_url()
    executor = get_executor(resolved_db_url)
    service = build_service(executor, cubes_dir=cubes_dir, demo=demo, planner=planner, tier=tier)
    resolved_auth = auth if auth is not None else TokenAuth.from_env()

    server = ThreadingHTTPServer((host, port), NexDashRequestHandler)
    server.executor = executor  # type: ignore[attr-defined]
    server.service = service  # type: ignore[attr-defined]
    server.mcp = MCPServer(service)  # type: ignore[attr-defined]
    server.auth = resolved_auth  # type: ignore[attr-defined]

    logger.info("NexDash server starting on %s:%d", host, server.server_address[1])
    logger.info("Database backend: %s (dialect: %s)", sanitize_db_url(resolved_db_url), executor.dialect_name)
    logger.info("Cubes: %s", ", ".join(service.registry.list_cubes()) or "none")
    if resolved_auth.allow_anonymous:
        logger.warning("Anonymous access is enabled (%s); use it only for local demos.", ALLOW_ANONYMOUS_ENV)
    elif not resolved_auth.configured:
        logger.warning("%s is not set; protected endpoints will answer 401.", TOKEN_ENV)
    return server


def run_mcp_stdio(
    db_url: str | None = None,
    *,
    cubes_dir: str | Path | None = None,
    demo: bool = False,
    tier: str = DEFAULT_TIER,
) -> None:
    """Serve the MCP tools over stdin/stdout until stdin closes."""
    _configure_logging()
    executor = get_executor(db_url or get_db_url())
    try:
        service = build_service(executor, cubes_dir=cubes_dir, demo=demo, tier=tier)
        MCPServer(service).serve_stdio()
    finally:
        executor.close()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main(args: list[str] | None = None) -> None:
    """CLI entrypoint: HTTP server by default, MCP over stdio with ``--mcp``."""
    parser = argparse.ArgumentParser(description="NexDash semantic query server (REST, MCP and web UI)")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104
        help="Host address to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DB_URL"),
        help="Database connection URL (defaults to DB_URL environment variable or SQLite :memory:)",
    )
    parser.add_argument(
        "--cubes",
        default=os.environ.get("NEXDASH_CUBES_DIR"),
        help="Directory with cube YAML files (default: NEXDASH_CUBES_DIR)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=_env_flag("NEXDASH_DEMO"),
        help="Load the synthetic sales, support and stock cubes with generated data (SQLite only)",
    )
    parser.add_argument(
        "--tier",
        choices=TIERS,
        default=os.environ.get("NEXDASH_LLM_TIER", DEFAULT_TIER),
        help="nexcoder-core model tier used to answer questions (default: medium)",
    )
    parser.add_argument("--mcp", action="store_true", help="Serve MCP over stdio instead of HTTP")

    parsed = parser.parse_args(args)
    if parsed.mcp:
        run_mcp_stdio(parsed.db_url, cubes_dir=parsed.cubes, demo=parsed.demo, tier=parsed.tier)
        return

    server = run_server(
        host=parsed.host,
        port=parsed.port,
        db_url=parsed.db_url,
        cubes_dir=parsed.cubes,
        demo=parsed.demo,
        tier=parsed.tier,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server interrupted; shutting down.")
    finally:
        server.server_close()
        if hasattr(server, "executor"):
            server.executor.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main(sys.argv[1:])
