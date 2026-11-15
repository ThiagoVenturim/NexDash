"""Lightweight HTTP server exposing health check, query execution, and dashboard serving.

Zero external web framework dependencies; uses Python standard library http.server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nexdash import __version__
from nexdash.db import get_db_url, sanitize_db_url
from nexdash.executor import get_executor
from nexdash.sources import validate_select_sql

logger = logging.getLogger("nexdash.server")


class NexDashRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health and analytical queries."""

    server_version = f"NexDash/{__version__}"

    def do_GET(self) -> None:  # noqa: N802
        parsed_path = self.path.split("?")[0]
        if parsed_path in ("/health", "/api/health"):
            self._handle_health()
        else:
            self._send_json({"error": "Not Found", "path": self.path}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed_path = self.path.split("?")[0]
        if parsed_path in ("/api/query", "/query"):
            self._handle_query()
        else:
            self._send_json({"error": "Not Found", "path": self.path}, status=HTTPStatus.NOT_FOUND)

    def _handle_health(self) -> None:
        db_url = get_db_url()
        executor = getattr(self.server, "executor", None)
        dialect = executor.dialect_name if executor else "unknown"

        payload = {
            "status": "ok",
            "version": __version__,
            "database": sanitize_db_url(db_url),
            "dialect": dialect,
        }
        self._send_json(payload)

    def _handle_query(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self._send_json({"error": "Empty request body"}, status=HTTPStatus.BAD_REQUEST)
            return

        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            self._send_json({"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
            return

        sql = data.get("sql")
        params = data.get("params") or {}
        if not sql:
            self._send_json({"error": "Missing 'sql' in query payload"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            validated_sql = validate_select_sql(sql)
            executor = getattr(self.server, "executor", None)
            if executor is None:
                self._send_json({"error": "No database executor available"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            result = executor.execute_sql(validated_sql, params)
            self._send_json(
                {
                    "columns": result.columns,
                    "rows": result.rows,
                    "row_count": result.row_count,
                }
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)


def run_server(
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8000,
    db_url: str | None = None,
) -> ThreadingHTTPServer:
    """Instantiate and start the NexDash HTTP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    resolved_db_url = db_url or get_db_url()
    sanitized = sanitize_db_url(resolved_db_url)
    executor = get_executor(resolved_db_url)

    server = ThreadingHTTPServer((host, port), NexDashRequestHandler)
    server.executor = executor  # type: ignore[attr-defined]

    logger.info("NexDash server starting on %s:%d", host, port)
    logger.info("Database backend: %s (dialect: %s)", sanitized, executor.dialect_name)
    return server


def main(args: list[str] | None = None) -> None:
    """CLI entrypoint parsing host and port arguments and starting the server."""
    parser = argparse.ArgumentParser(description="NexDash semantic query and dashboard server")
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

    parsed = parser.parse_args(args)
    server = run_server(host=parsed.host, port=parsed.port, db_url=parsed.db_url)
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
