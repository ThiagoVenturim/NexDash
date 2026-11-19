"""Bearer token authentication with per-operation scopes.

Tokens are read from environment variables (or a ``.env`` file next to the
working directory) and are never stored in code, configuration files under
version control, or logs. Requests send a token as ``Authorization: Bearer
<token>`` or ``X-API-Key: <token>``; query strings are not accepted because they
end up in access logs and browser history.

Two kinds of token exist:

- ``NEXDASH_API_TOKEN``: the operator token. It holds every scope, including
  ``admin`` (guarded read-only SQL on ``/api/query``).
- Client tokens, one per consuming project: ``NEXDASH_CLIENT_<NAME>_TOKEN`` plus
  ``NEXDASH_CLIENT_<NAME>_SCOPES`` (comma-separated, see ``SCOPES``). For example
  ``NEXDASH_CLIENT_NEXCODER_TOKEN`` with ``NEXDASH_CLIENT_NEXCODER_SCOPES=reports:list,reports:run``.
  A client token without a scopes variable gets no scope at all.

Without any token every protected endpoint answers 401, unless anonymous access
is switched on explicitly with ``NEXDASH_ALLOW_ANONYMOUS=1`` (local demos only).
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from nexdash.db import load_env_file

TOKEN_ENV = "NEXDASH_API_TOKEN"
ALLOW_ANONYMOUS_ENV = "NEXDASH_ALLOW_ANONYMOUS"
CLIENT_ENV_PREFIX = "NEXDASH_CLIENT_"
MIN_TOKEN_LENGTH = 16

SCOPE_SEMANTIC_READ = "semantic:read"
SCOPE_SEMANTIC_QUERY = "semantic:query"
SCOPE_SEMANTIC_ASK = "semantic:ask"
SCOPE_REPORTS_LIST = "reports:list"
SCOPE_REPORTS_RUN = "reports:run"
SCOPE_ADMIN = "admin"

SCOPES: dict[str, str] = {
    SCOPE_SEMANTIC_READ: "List and describe cubes",
    SCOPE_SEMANTIC_QUERY: "Run structured query plans against cubes",
    SCOPE_SEMANTIC_ASK: "Ask questions in natural language (uses a model tier)",
    SCOPE_REPORTS_LIST: "List and describe report models",
    SCOPE_REPORTS_RUN: "Generate report data and dashboards from a model and parameters",
    SCOPE_ADMIN: "Guarded read-only SQL; reserved for the operator token",
}
ALL_SCOPES = frozenset(SCOPES)
CLIENT_SCOPES = ALL_SCOPES - {SCOPE_ADMIN}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_CLIENT_TOKEN_RE = re.compile(rf"^{CLIENT_ENV_PREFIX}([A-Z0-9][A-Z0-9_]*)_TOKEN$")


class AuthConfigError(ValueError):
    """The configured token is unusable (for example too short) or a scope is unknown."""


@dataclass(frozen=True)
class Principal:
    """Who is calling and which scopes it holds. Never carries the token itself."""

    name: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class _Client:
    name: str
    token: bytes
    scopes: frozenset[str]

    def __repr__(self) -> str:
        return f"_Client(name={self.name!r}, scopes={sorted(self.scopes)!r})"


def _check_length(token: str, label: str) -> None:
    if len(token) < MIN_TOKEN_LENGTH:
        raise AuthConfigError(
            f"{label} must have at least {MIN_TOKEN_LENGTH} characters; "
            "generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


def parse_scopes(value: str | Iterable[str] | None, *, label: str = "scopes") -> frozenset[str]:
    """Parse a comma-separated scope list. Unknown scopes and ``admin`` for clients are rejected."""
    if value is None:
        return frozenset()
    items = value.split(",") if isinstance(value, str) else list(value)
    scopes = frozenset(item.strip() for item in items if item and item.strip())
    unknown = sorted(scopes - CLIENT_SCOPES)
    if unknown:
        raise AuthConfigError(f"{label}: unknown or reserved scopes {unknown}; allowed: {sorted(CLIENT_SCOPES)}")
    return scopes


class TokenAuth:
    """Constant-time check of the operator token and of scoped client tokens."""

    def __init__(
        self,
        token: str | None,
        *,
        allow_anonymous: bool = False,
        clients: Mapping[str, tuple[str, Iterable[str] | str]] | None = None,
    ) -> None:
        cleaned = (token or "").strip()
        if cleaned:
            _check_length(cleaned, TOKEN_ENV)
        self._token = cleaned or None
        self._clients: list[_Client] = []
        seen: set[str] = set()
        for name, (client_token, scopes) in (clients or {}).items():
            client_name = str(name).strip().lower()
            if not client_name:
                raise AuthConfigError("Client names must not be empty")
            if client_name in seen or client_name in {"operator", "anonymous"}:
                raise AuthConfigError(f"Client name '{client_name}' is duplicated or reserved")
            seen.add(client_name)
            value = (client_token or "").strip()
            _check_length(value, f"Token of client '{client_name}'")
            if self._token is not None and hmac.compare_digest(value.encode("utf-8"), self._token.encode("utf-8")):
                raise AuthConfigError(f"Client '{client_name}' must not reuse {TOKEN_ENV}")
            self._clients.append(
                _Client(client_name, value.encode("utf-8"), parse_scopes(scopes, label=f"client '{client_name}'"))
            )
        self.allow_anonymous = allow_anonymous and not self.configured

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> TokenAuth:
        """Build from ``NEXDASH_API_TOKEN``, ``NEXDASH_CLIENT_<NAME>_TOKEN``/``_SCOPES`` and
        ``NEXDASH_ALLOW_ANONYMOUS`` (environment or .env)."""
        load_env_file(dotenv_path)
        token = os.environ.get(TOKEN_ENV)
        anonymous = os.environ.get(ALLOW_ANONYMOUS_ENV, "").strip().lower() in _TRUE_VALUES
        clients: dict[str, tuple[str, str]] = {}
        for key in sorted(os.environ):
            match = _CLIENT_TOKEN_RE.match(key)
            if not match or not os.environ[key].strip():
                continue
            name = match.group(1)
            scopes = os.environ.get(f"{CLIENT_ENV_PREFIX}{name}_SCOPES", "")
            clients[name.lower()] = (os.environ[key], scopes)
        return cls(token, allow_anonymous=anonymous, clients=clients)

    @property
    def configured(self) -> bool:
        return self._token is not None or bool(self._clients)

    @property
    def client_names(self) -> list[str]:
        return [client.name for client in self._clients]

    def identify(self, provided: str | None) -> Principal | None:
        """Return the caller for ``provided``, or None when the token matches nothing.

        Every configured token is compared, so the time taken does not reveal which one matched.
        """
        if not self.configured:
            return Principal("anonymous", ALL_SCOPES) if self.allow_anonymous else None
        if not provided:
            return None
        raw = provided.encode("utf-8")
        found: Principal | None = None
        if self._token is not None and hmac.compare_digest(raw, self._token.encode("utf-8")):
            found = Principal("operator", ALL_SCOPES)
        for client in self._clients:
            if hmac.compare_digest(raw, client.token) and found is None:
                found = Principal(client.name, client.scopes)
        return found

    def identify_headers(self, headers: Mapping[str, str] | object) -> Principal | None:
        return self.identify(extract_token(headers))

    def verify(self, provided: str | None) -> bool:
        """Return True when ``provided`` matches a configured token (or anonymous access is allowed)."""
        return self.identify(provided) is not None

    def verify_headers(self, headers: Mapping[str, str] | object) -> bool:
        return self.verify(extract_token(headers))

    def __repr__(self) -> str:
        state = "configured" if self._token else ("anonymous" if self.allow_anonymous else "missing")
        return f"TokenAuth(token={state}, clients={self.client_names!r})"


def extract_token(headers: Mapping[str, str] | object) -> str | None:
    """Read the token from ``Authorization: Bearer`` or ``X-API-Key`` headers."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    auth = getter("Authorization") or getter("authorization") or ""
    if isinstance(auth, str) and auth[:7].lower() == "bearer ":
        value = auth[7:].strip()
        if value:
            return value
    api_key = getter("X-API-Key") or getter("x-api-key")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    return None


__all__ = [
    "ALLOW_ANONYMOUS_ENV",
    "ALL_SCOPES",
    "CLIENT_ENV_PREFIX",
    "CLIENT_SCOPES",
    "MIN_TOKEN_LENGTH",
    "SCOPES",
    "SCOPE_ADMIN",
    "SCOPE_REPORTS_LIST",
    "SCOPE_REPORTS_RUN",
    "SCOPE_SEMANTIC_ASK",
    "SCOPE_SEMANTIC_QUERY",
    "SCOPE_SEMANTIC_READ",
    "TOKEN_ENV",
    "AuthConfigError",
    "Principal",
    "TokenAuth",
    "extract_token",
    "parse_scopes",
]
