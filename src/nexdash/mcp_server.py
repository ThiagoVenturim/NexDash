"""Model Context Protocol server exposing the semantic layer as tools.

Implements the JSON-RPC 2.0 subset of MCP that tool servers need
(``initialize``, ``ping``, ``tools/list``, ``tools/call``) without extra
dependencies. Two transports are available:

- stdio (``nexdash --mcp``): one JSON message per line on stdin/stdout, for
  clients that launch nexdash as a local subprocess. The process runs with the
  caller's own privileges, so no token is checked; logs go to stderr.
- HTTP (``POST /mcp`` on the REST server): the same handler behind the bearer
  token check of the REST API.

Tools:

- ``nexdash_list_cubes``: cubes with their dimensions and metrics.
- ``nexdash_describe_cube``: dimensions, metrics and allowed filters of a cube.
- ``nexdash_query``: run a structured ``QueryPlan`` (no free SQL).
- ``nexdash_ask``: turn a question into a plan with the configured model and run it.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from nexdash import __version__
from nexdash.plan import QueryPlan
from nexdash.service import QueryService, ServiceError

logger = logging.getLogger("nexdash.mcp")

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _tool_definitions() -> list[dict[str, Any]]:
    plan_schema = QueryPlan.model_json_schema()
    return [
        {
            "name": "nexdash_list_cubes",
            "description": "List the semantic cubes available for analytics, with their dimensions and metrics.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "nexdash_describe_cube",
            "description": "Describe one cube: dimensions, metrics and the filter operators allowed per field.",
            "inputSchema": {
                "type": "object",
                "properties": {"cube": {"type": "string", "description": "Cube name"}},
                "required": ["cube"],
                "additionalProperties": False,
            },
        },
        {
            "name": "nexdash_query",
            "description": (
                "Run a structured query plan against a cube. Fields must come from the cube description; "
                "the plan is compiled to parameterized SQL through an allowlist."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"plan": plan_schema},
                "required": ["plan"],
                "additionalProperties": False,
            },
        },
        {
            "name": "nexdash_ask",
            "description": "Answer an analytics question in natural language: the server builds and runs the plan.",
            "inputSchema": {
                "type": "object",
                "properties": {"question": {"type": "string", "maxLength": 2000}},
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    ]


class MCPServer:
    """Dispatches MCP JSON-RPC messages to a ``QueryService``."""

    def __init__(self, service: QueryService) -> None:
        self.service = service
        self._tools = _tool_definitions()

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    def handle(self, message: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Handle one JSON-RPC message or batch. Returns None for notifications."""
        if isinstance(message, list):
            if not message:
                return _error(None, INVALID_REQUEST, "Empty batch")
            replies = [reply for reply in (self._handle_one(item) for item in message) if reply is not None]
            return replies or None
        return self._handle_one(message)

    def _handle_one(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            req_id = message.get("id") if isinstance(message, dict) else None
            return _error(req_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        method = message.get("method")
        is_notification = "id" not in message
        req_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return None if is_notification else _error(req_id, INVALID_PARAMS, "params must be an object")

        if is_notification:
            # notifications/initialized, notifications/cancelled and others need no reply.
            return None

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
            return _result(
                req_id,
                {
                    "protocolVersion": version,
                    "serverInfo": {"name": "nexdash", "version": __version__},
                    "capabilities": {"tools": {"listChanged": False}},
                    "instructions": (
                        "Call nexdash_list_cubes and nexdash_describe_cube first, then nexdash_query "
                        "with a plan that uses only the listed fields."
                    ),
                },
            )
        if method == "ping":
            return _result(req_id, {})
        if method == "tools/list":
            return _result(req_id, {"tools": self.tools})
        if method == "tools/call":
            return self._call_tool(req_id, params)
        return _error(req_id, METHOD_NOT_FOUND, f"Unknown method '{method}'")

    def _call_tool(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(req_id, INVALID_PARAMS, "arguments must be an object")

        try:
            if name == "nexdash_list_cubes":
                payload: Any = {"cubes": self.service.list_cubes()}
            elif name == "nexdash_describe_cube":
                payload = self.service.describe_cube(arguments.get("cube", ""))
            elif name == "nexdash_query":
                payload = self.service.run_plan(arguments.get("plan"))
            elif name == "nexdash_ask":
                payload = self.service.ask(arguments.get("question"))
            else:
                return _error(req_id, INVALID_PARAMS, f"Unknown tool '{name}'")
        except ServiceError as exc:
            return _result(req_id, _tool_content({"error": str(exc)}, is_error=True))
        except Exception as exc:
            logger.error("Tool '%s' failed: %s", name, type(exc).__name__)
            return _result(req_id, _tool_content({"error": "Internal error"}, is_error=True))

        return _result(req_id, _tool_content(payload))

    def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        """Serve newline-delimited JSON-RPC messages until stdin closes."""
        reader = stdin or sys.stdin
        writer = stdout or sys.stdout
        for line in reader:
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                reply: Any = _error(None, PARSE_ERROR, "Parse error")
            else:
                reply = self.handle(message)
            if reply is not None:
                writer.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
                writer.flush()


def _tool_content(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(payload, dict) and not is_error:
        result["structuredContent"] = json.loads(text)
    return result


def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


__all__ = ["LATEST_PROTOCOL_VERSION", "SUPPORTED_PROTOCOL_VERSIONS", "MCPServer"]
