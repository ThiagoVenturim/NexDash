"""Helpers shared by the report engine examples (offline, synthetic data only).

- ``local_server``: start nexdash on 127.0.0.1 with the synthetic report model data and
  a freshly generated client token that holds only the report scopes.
- ``check_dashboard``: minimal validity checks a client can run on the HTML it receives.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser

from nexdash.auth import SCOPE_REPORTS_LIST, SCOPE_REPORTS_RUN, TokenAuth
from nexdash.server import run_server

CLIENT_NAME = "example"
CLIENT_SCOPES = f"{SCOPE_REPORTS_LIST},{SCOPE_REPORTS_RUN}"


@dataclass(frozen=True)
class LocalServer:
    url: str
    token: str


@contextlib.contextmanager
def local_server() -> Iterator[LocalServer]:
    """Run a local nexdash server for the duration of the block.

    The token only lives in this process. A real deployment reads it from
    NEXDASH_CLIENT_<NAME>_TOKEN in the environment or .env instead.
    """
    token = secrets.token_urlsafe(32)
    auth = TokenAuth(None, clients={CLIENT_NAME: (token, CLIENT_SCOPES)})
    server = run_server("127.0.0.1", 0, "sqlite:///:memory:", demo=True, report_models=True, auth=auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LocalServer(url=f"http://127.0.0.1:{server.server_address[1]}", token=token)
    finally:
        server.shutdown()
        server.server_close()
        server.executor.close()


class _DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: dict[str, int] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags[tag] = self.tags.get(tag, 0) + 1
        # Only the document title; inline SVG charts carry their own <title> elements.
        self._in_title = tag == "title" and not self.tags.get("body")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def check_dashboard(html: str, *, title: str | None = None) -> list[str]:
    """Return the problems found in a report dashboard (an empty list means it is valid)."""
    problems: list[str] = []
    if not html.lower().startswith("<!doctype html>"):
        problems.append("missing <!doctype html>")
    parser = _DashboardParser()
    parser.feed(html)
    parser.close()
    for tag in ("html", "head", "body", "main"):
        if parser.tags.get(tag) != 1:
            problems.append(f"expected exactly one <{tag}>")
    if parser.tags.get("script"):
        problems.append("dashboard contains <script>")
    if not parser.tags.get("svg"):
        problems.append("dashboard has no chart")
    if title is not None and parser.title.strip() != title:
        problems.append(f"title is {parser.title.strip()!r}, expected {title!r}")
    return problems
