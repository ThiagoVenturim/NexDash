"""Call the nexdash report engine over MCP and receive a dashboard.

Two transports, both offline with synthetic data:

- stdio (default): launches ``python -m nexdash --mcp --demo --report-models`` as a
  subprocess and talks newline-delimited JSON-RPC on its stdin and stdout, like a
  desktop MCP client does.
- HTTP (``--http``): starts a local nexdash on 127.0.0.1 and posts the same messages
  to ``/mcp`` with a client token that holds only the report scopes.

    python examples/report_engine/mcp_client.py
    python examples/report_engine/mcp_client.py --http --out dashboards/
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from engine_example_support import check_dashboard, local_server

PROTOCOL_VERSION = "2025-06-18"

ARGUMENTS = {
    "model": "dev_productivity",
    "parameters": {"start": "2026-04-01", "end": "2026-06-30", "team": "platform"},
    "output": "both",
}

Send = Callable[[dict[str, Any]], dict[str, Any] | None]


def run_session(send: Send) -> dict[str, Any]:
    """Initialize, list tools, run a report and try an invalid call, using ``send`` as transport."""
    ids = itertools.count(1)

    def request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = send({"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params or {}})
        if reply is None or "error" in reply:
            raise RuntimeError(f"{method} failed: {reply}")
        return reply["result"]

    init = request(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "example", "version": "1"}},
    )
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = [tool["name"] for tool in request("tools/list")["tools"]]
    models = request("tools/call", {"name": "nexdash_list_report_models", "arguments": {}})
    report = request("tools/call", {"name": "nexdash_run_report", "arguments": ARGUMENTS})
    invalid = request(
        "tools/call",
        {"name": "nexdash_run_report", "arguments": {"model": "dev_productivity", "parameters": {"team": "x" * 500}}},
    )

    payload = report["structuredContent"]
    resources = [item["resource"] for item in report["content"] if item.get("type") == "resource"]
    html = resources[0]["text"] if resources else ""
    return {
        "server": init["serverInfo"],
        "tools": tools,
        "models": [model["name"] for model in models["structuredContent"]["models"]],
        "report": payload,
        "report_is_error": report["isError"],
        "dashboard_html": html,
        "dashboard_problems": check_dashboard(html, title=payload.get("title")),
        "invalid_is_error": invalid["isError"],
        "invalid_message": json.loads(invalid["content"][0]["text"])["error"],
    }


def run_stdio() -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("NEXDASH_")}
    env["DB_URL"] = "sqlite:///:memory:"
    command = [sys.executable, "-m", "nexdash", "--mcp", "--demo", "--report-models"]
    with subprocess.Popen(  # noqa: S603 - fixed command, no shell
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env,
    ) as process:
        assert process.stdin is not None and process.stdout is not None

        def send(message: dict[str, Any]) -> dict[str, Any] | None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" not in message:
                return None
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("The MCP server closed its output")
            return json.loads(line)

        try:
            return run_session(send)
        finally:
            process.stdin.close()
            process.wait(timeout=30)


def run_http() -> dict[str, Any]:
    with local_server() as server:
        headers = {"Authorization": f"Bearer {server.token}"}
        with httpx.Client(base_url=server.url, headers=headers, timeout=30, trust_env=False) as client:

            def send(message: dict[str, Any]) -> dict[str, Any] | None:
                response = client.post("/mcp", json=message)
                response.raise_for_status()
                return response.json() if response.status_code == 200 else None

            return run_session(send)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--http", action="store_true", help="Use MCP over HTTP instead of stdio")
    parser.add_argument("--out", type=Path, help="Directory where the dashboard HTML is written")
    args = parser.parse_args(argv)

    result = run_http() if args.http else run_stdio()
    report = result["report"]
    print(f"Server: {result['server']['name']} {result['server']['version']}")
    print(f"Tools: {', '.join(result['tools'])}")
    print(f"Report: {report['title']} ({report['filters']})")
    print(f"Dashboard: {len(result['dashboard_html'])} bytes, problems: {result['dashboard_problems'] or 'none'}")
    print(f"Invalid parameters refused: {result['invalid_message']}")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        target = args.out / f"{report['model']}.html"
        target.write_text(result["dashboard_html"], encoding="utf-8")
        print(f"Wrote {target}")
    return 0 if not result["dashboard_problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
