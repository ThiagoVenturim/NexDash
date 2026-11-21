"""Shared HTTP machinery for data source connectors.

Every connector gets the same guarantees from ``BaseConnector``:

- Credentials come from environment variables (or a ``.env`` file loaded with
  ``nexdash.db.load_env_file``). There is no parameter that accepts a literal
  token, so tokens cannot end up in code or in YAML committed to the repository.
- Requests only go to the configured origin. Pagination links that point to
  another host are rejected, and redirects are never followed, so a token cannot
  leak to a third party.
- The base host is checked against loopback and cloud metadata addresses with
  ``nexdash.sources.check_ssrf_host``.
- Rate limits (HTTP 429, 503, or 403 with an exhausted quota header) are retried
  after ``Retry-After`` or the reset header, up to ``max_retries``.
- GET responses are cached in memory for ``cache_ttl_s`` seconds.
- Error messages and log lines go through ``mask_secrets``; headers are never logged.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from nexdash.connectors.models import WorkItem
from nexdash.db import load_env_file
from nexdash.sources import (
    MAX_RESPONSE_BYTES,
    SourceValidationError,
    check_ssrf_host,
    mask_secrets,
    validate_url,
)

logger = logging.getLogger("nexdash.connectors")

DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_WAIT_S = 60.0
DEFAULT_CACHE_TTL_S = 60.0
DEFAULT_TIMEOUT_S = 20.0

ResolverFn = Callable[[str], list[str]]


class ConnectorError(Exception):
    """Base error for connectors. Messages never contain credential values."""


class ConnectorConfigError(ConnectorError, ValueError):
    """Invalid connector configuration or missing credential variable."""


class ConnectorAuthError(ConnectorError):
    """The remote API rejected the credential (HTTP 401 or 403)."""


class ConnectorHTTPError(ConnectorError):
    """The remote API answered with an unexpected HTTP status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ConnectorRateLimitError(ConnectorError):
    """The remote API rate limit is exhausted and retries were used up."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def resolve_credential(
    env_var: str,
    *,
    env: Mapping[str, str] | None = None,
    required: bool = True,
) -> str | None:
    """Read a credential from ``env`` (when given) or from the process environment and ``.env``.

    Raises ConnectorConfigError naming the variable, never its value, when it is missing.
    """
    if env is None:
        load_env_file()
        env = os.environ
    value = (env.get(env_var) or "").strip()
    if not value:
        if required:
            raise ConnectorConfigError(
                f"Credential variable '{env_var}' is not set; define it in the environment or in .env."
            )
        return None
    return value


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


class BaseConnector(ABC):
    """Base class for paginated, authenticated, rate-limit aware connectors."""

    kind: ClassVar[str] = "base"

    def __init__(
        self,
        base_url: str,
        *,
        env: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        verify_host: bool = True,
        allow_private: bool = True,
        resolver: ResolverFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            cleaned = validate_url(base_url).rstrip("/")
        except SourceValidationError as exc:
            raise ConnectorConfigError(str(exc)) from exc
        if page_size < 1:
            raise ConnectorConfigError("page_size must be at least 1.")
        if max_pages < 1:
            raise ConnectorConfigError("max_pages must be at least 1.")
        if max_retries < 0:
            raise ConnectorConfigError("max_retries cannot be negative.")
        if verify_host:
            try:
                check_ssrf_host(urlsplit(cleaned).hostname or "", allow_private=allow_private, resolver=resolver)
            except SourceValidationError as exc:
                raise ConnectorConfigError(str(exc)) from exc

        self.base_url = cleaned
        self.env = env
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_retries = max_retries
        self.max_wait_s = max_wait_s
        self.cache_ttl_s = cache_ttl_s
        self.truncated = False
        self._sleep = sleep
        self._clock = clock
        self._secrets: dict[str, str] = {}
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, httpx.Response]] = {}
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- credentials -------------------------------------------------------

    def _credential(self, env_var: str, *, required: bool = True) -> str | None:
        value = resolve_credential(env_var, env=self.env, required=required)
        if value:
            self._remember_secret(env_var, value)
        return value

    def _remember_secret(self, name: str, value: str) -> None:
        self._secrets[name] = value

    def _mask(self, text: str) -> str:
        return mask_secrets(text, self._secrets)

    @abstractmethod
    def auth_headers(self) -> dict[str, str]:
        """Headers that authenticate every request."""

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "User-Agent": "nexdash-connectors"}

    # -- HTTP --------------------------------------------------------------

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _ensure_same_origin(self, url: str) -> None:
        if _origin(url) != _origin(self.base_url):
            raise ConnectorError(
                self._mask(f"Refusing request to '{urlsplit(url).hostname}': outside the configured base URL.")
            )

    def _rate_limit_wait(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(retry_after).timestamp() - self._clock())
                except (TypeError, ValueError):
                    pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(0.0, float(reset) - self._clock())
            except ValueError:
                pass
        return float(2**attempt)

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code in (429, 503):
            return True
        return response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """Send one request with auth, same-origin check, retries on rate limit and caching for GET."""
        self._ensure_same_origin(url)
        method = method.upper()
        clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None}
        cache_key = (url, tuple(sorted(clean_params.items())))
        if method == "GET" and self.cache_ttl_s > 0:
            cached = self._cache.get(cache_key)
            if cached and self._clock() - cached[0] < self.cache_ttl_s:
                return cached[1]

        headers = {**self.default_headers(), **self.auth_headers()}
        path_for_log = self._mask(urlsplit(url).path)
        attempt = 0
        while True:
            logger.debug("%s %s (attempt %d)", method, path_for_log, attempt + 1)
            try:
                response = self._client.request(
                    method, url, params=clean_params or None, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                raise ConnectorError(self._mask(f"{self.kind}: request to {path_for_log} failed: {exc}")) from None

            if self._is_rate_limited(response):
                wait = self._rate_limit_wait(response, attempt)
                if attempt >= self.max_retries or wait > self.max_wait_s:
                    raise ConnectorRateLimitError(
                        f"{self.kind}: rate limit reached on {path_for_log} (HTTP {response.status_code}).",
                        retry_after_s=wait,
                    )
                logger.info("%s: rate limited, waiting %.1fs", self.kind, wait)
                self._sleep(wait)
                attempt += 1
                continue
            break

        status = response.status_code
        if status in (401, 403):
            raise ConnectorAuthError(f"{self.kind}: credential rejected on {path_for_log} (HTTP {status}).")
        if 300 <= status < 400:
            raise ConnectorHTTPError(
                f"{self.kind}: redirect on {path_for_log} is not followed (HTTP {status}).", status
            )
        if status >= 400:
            raise ConnectorHTTPError(f"{self.kind}: {path_for_log} answered HTTP {status}.", status)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ConnectorError(f"{self.kind}: response from {path_for_log} exceeds {MAX_RESPONSE_BYTES} bytes.")

        if method == "GET" and self.cache_ttl_s > 0:
            self._cache[cache_key] = (self._clock(), response)
        return response

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        method: str = "GET",
        json_body: Any = None,
    ) -> tuple[Any, httpx.Response]:
        response = self.request(method, url, params=params, json_body=json_body)
        try:
            return response.json(), response
        except ValueError:
            raise ConnectorError(
                f"{self.kind}: {self._mask(urlsplit(url).path)} did not return valid JSON."
            ) from None

    def _check_page_budget(self, pages_read: int) -> bool:
        """Return True while more pages may be read; flag truncation once max_pages is hit."""
        if pages_read >= self.max_pages:
            self.truncated = True
            logger.warning("%s: stopped after %d pages (max_pages)", self.kind, self.max_pages)
            return False
        return True

    # -- items -------------------------------------------------------------

    @abstractmethod
    def iter_items(self) -> Iterator[WorkItem]:
        """Yield normalized work items, following pagination."""

    def fetch_items(self, *, limit: int | None = None) -> list[WorkItem]:
        """Collect normalized work items, optionally stopping after ``limit`` items."""
        self.truncated = False
        items: list[WorkItem] = []
        for item in self.iter_items():
            items.append(item)
            if limit is not None and len(items) >= limit:
                break
        return items

    def fetch_records(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Collect work items as flat dicts ready for ``load_records``."""
        return [item.to_record() for item in self.fetch_items(limit=limit)]
