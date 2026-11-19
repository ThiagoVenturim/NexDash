"""Bearer token authentication for the REST API, the HTTP MCP endpoint and the web UI.

The token is read from the ``NEXDASH_API_TOKEN`` environment variable (or a
``.env`` file next to the working directory) and is never stored in code,
configuration files under version control, or logs. Requests send it as
``Authorization: Bearer <token>`` or ``X-API-Key: <token>``; query strings are
not accepted because they end up in access logs and browser history.

Without a token every protected endpoint answers 401, unless anonymous access
is switched on explicitly with ``NEXDASH_ALLOW_ANONYMOUS=1`` (local demos only).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from pathlib import Path

from nexdash.db import load_env_file

TOKEN_ENV = "NEXDASH_API_TOKEN"
ALLOW_ANONYMOUS_ENV = "NEXDASH_ALLOW_ANONYMOUS"
MIN_TOKEN_LENGTH = 16

_TRUE_VALUES = {"1", "true", "yes", "on"}


class AuthConfigError(ValueError):
    """The configured token is unusable (for example too short)."""


class TokenAuth:
    """Constant-time check of a shared API token."""

    def __init__(self, token: str | None, *, allow_anonymous: bool = False) -> None:
        cleaned = (token or "").strip()
        if cleaned and len(cleaned) < MIN_TOKEN_LENGTH:
            raise AuthConfigError(
                f"{TOKEN_ENV} must have at least {MIN_TOKEN_LENGTH} characters; "
                "generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        self._token = cleaned or None
        self.allow_anonymous = allow_anonymous and self._token is None

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> TokenAuth:
        """Build from ``NEXDASH_API_TOKEN`` and ``NEXDASH_ALLOW_ANONYMOUS`` (environment or .env)."""
        load_env_file(dotenv_path)
        token = os.environ.get(TOKEN_ENV)
        anonymous = os.environ.get(ALLOW_ANONYMOUS_ENV, "").strip().lower() in _TRUE_VALUES
        return cls(token, allow_anonymous=anonymous)

    @property
    def configured(self) -> bool:
        return self._token is not None

    def verify(self, provided: str | None) -> bool:
        """Return True when ``provided`` matches the token (or anonymous access is allowed)."""
        if self._token is None:
            return self.allow_anonymous
        if not provided:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), self._token.encode("utf-8"))

    def verify_headers(self, headers: Mapping[str, str] | object) -> bool:
        return self.verify(extract_token(headers))

    def __repr__(self) -> str:
        state = "configured" if self._token else ("anonymous" if self.allow_anonymous else "missing")
        return f"TokenAuth(token={state})"


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
    "MIN_TOKEN_LENGTH",
    "TOKEN_ENV",
    "AuthConfigError",
    "TokenAuth",
    "extract_token",
]
