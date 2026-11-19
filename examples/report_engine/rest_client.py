"""Call the nexdash report engine over REST and receive a dashboard.

Offline by default: starts a local nexdash on 127.0.0.1 with synthetic data and a
generated client token, then plays the role of a consuming project.

    python examples/report_engine/rest_client.py
    python examples/report_engine/rest_client.py --out dashboards/

Against a running server, pass its URL; the token comes from NEXDASH_ENGINE_TOKEN
(environment or .env), never from the command line:

    python examples/report_engine/rest_client.py --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from engine_example_support import check_dashboard, local_server

from nexdash.db import load_env_file

TOKEN_ENV = "NEXDASH_ENGINE_TOKEN"

REQUEST = {
    "model": "support_metrics",
    "parameters": {"start": "2026-04-01", "end": "2026-06-30", "team": "tier1"},
    "output": "both",
    "locale": "en",
}


def call_engine(url: str, token: str) -> dict[str, Any]:
    """List models, run one, fetch its dashboard and show that bad parameters are refused."""
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=url, headers=headers, timeout=30, trust_env=False) as client:
        models = client.get("/api/reports/models")
        models.raise_for_status()
        names = [model["name"] for model in models.json()["models"]]

        described = client.get(f"/api/reports/models/{REQUEST['model']}")
        described.raise_for_status()

        report = client.post("/api/reports/run", json=REQUEST)
        report.raise_for_status()
        payload = report.json()

        page = client.post("/api/reports/dashboard", json=REQUEST)
        page.raise_for_status()

        invalid = client.post(
            "/api/reports/run",
            json={"model": REQUEST["model"], "parameters": {"start": "2026-06-30", "end": "2026-01-01"}},
        )
        sql_attempt = client.post(
            "/api/reports/run", json={"model": REQUEST["model"], "parameters": {"sql": "SELECT 1"}}
        )

    return {
        "models": names,
        "panels": [panel["id"] for panel in described.json()["panels"]],
        "report": payload,
        "dashboard_html": page.text,
        "dashboard_csp": page.headers.get("content-security-policy", ""),
        "dashboard_problems": check_dashboard(payload["dashboard"]["html"], title=payload["title"]),
        "rejected": {"invalid_period": invalid.status_code, "unknown_parameter": sql_attempt.status_code},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="URL of a running nexdash server (default: start a local one)")
    parser.add_argument("--out", type=Path, help="Directory where the dashboard HTML is written")
    args = parser.parse_args(argv)

    if args.url:
        load_env_file()
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            print(f"Set {TOKEN_ENV} in the environment or .env", file=sys.stderr)
            return 2
        result = call_engine(args.url, token)
    else:
        with local_server() as server:
            result = call_engine(server.url, server.token)

    report = result["report"]
    print(f"Models: {', '.join(result['models'])}")
    print(f"Report: {report['title']} ({report['filters']})")
    for panel_id, panel in report["data"]["panels"].items():
        value = panel["value"] if panel["kind"] == "kpi" else f"{len(panel['rows'])} rows"
        print(f"  {panel_id}: {value}")
    print(f"Dashboard: {len(result['dashboard_html'])} bytes, problems: {result['dashboard_problems'] or 'none'}")
    print(f"Refused: {result['rejected']}")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        target = args.out / f"{report['model']}.html"
        target.write_text(result["dashboard_html"], encoding="utf-8")
        print(f"Wrote {target}")
    return 0 if not result["dashboard_problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
