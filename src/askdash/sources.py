"""Data sources, security guards, and secret management for live dashboards.

Provides:
- Fernet secret encryption, normalization, merging, reference validation, and masking.
- SSRF guard blocking loopback, link-local (169.254.169.254), multicast, reserved,
  private networks, and single-label internal hostnames, re-checked on every redirect hop.
- SELECT-only SQL guard rejecting DDL/DML/stored procedures (DELETE, DROP, xp_*, etc.)
  and enforcing parameterized SQL execution.
- Data collection for HTTP, SQL (parameterized SELECT or semantic QueryPlan), CSV, and Sheets.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import decimal
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from askdash.compiler import SQLCompiler
from askdash.cube import Cube, CubeRegistry
from askdash.executor import SQLiteExecutor
from askdash.plan import QueryPlan

SOURCE_KINDS = ("http", "sql", "csv", "sheets")
MAX_SOURCES = 5
MAX_SECRETS = 20
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT_S = 20.0
DEFAULT_MAX_ROWS = 5000
MAX_ROWS_LIMIT = 20000
PREVIEW_SAMPLE_ROWS = 20

SOURCE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
ENV_REF_RE = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]{0,40})\s*\}\}")
TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_SENSITIVE_KEY_RE = re.compile(
    r"authorization|cookie|token|key|secret|senha|password|pwd|credential",
    re.IGNORECASE,
)
_FORBIDDEN_SQL_RE = re.compile(
    r"\b("
    r"insert|update|delete|merge|replace|upsert|"
    r"exec|execute|call|"
    r"drop|alter|create|truncate|rename|"
    r"grant|revoke|deny|"
    r"backup|restore|shutdown|dbcc|"
    r"into|openrowset|openquery|opendatasource|bulk|waitfor|"
    r"attach|detach|pragma|vacuum|"
    r"xp_\w+|sp_\w+"
    r")\b",
    re.IGNORECASE,
)
_GOOGLE_SHEETS_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{6,})")
_NUMBER_STD_RE = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")
_NUMBER_BR_RE = re.compile(r"^-?(0|[1-9]\d{0,2}(\.\d{3})*|[1-9]\d*),\d+$")

SECRET_MASK = "****"
_DEFAULT_MASTER_KEY = "askdash-local-default-fernet-master-key"
_HKDF_INFO = b"askdash-dashboard-sources-fernet-v1"


class SourceError(Exception):
    """Base error for dashboard data sources."""


class SourceValidationError(SourceError, ValueError):
    """Raised when a data source recipe or secret fails validation."""


class SSRFError(SourceValidationError):
    """Raised when a URL or resolved host violates SSRF protection rules."""


class SQLGuardError(SourceValidationError):
    """Raised when a SQL query violates the read-only parameterized SELECT guard."""


class SourceCollectionError(SourceError, RuntimeError):
    """Raised when collecting data from a source fails (with secrets masked)."""


ResolverFn = Callable[[str], list[str]]


@dataclass(frozen=True)
class HttpFetchResult:
    """Result of an HTTP fetch operation."""

    content: bytes
    content_type: str = ""
    encoding: str | None = "utf-8"
    status_code: int = 200
    headers: dict[str, str] | None = None


HttpFetcherFn = Callable[
    [str, str, dict[str, str], Any],
    HttpFetchResult,
]


# ---------------------------------------------------------------------------
# Secrets (Fernet encryption at rest)
# ---------------------------------------------------------------------------


def _build_fernet(secret_key: str | bytes | None = None) -> Fernet:
    """Derive a Fernet instance from a master secret key using HKDF-SHA256."""
    raw_key = (
        secret_key
        or os.environ.get("ASKDASH_SECRET_KEY")
        or os.environ.get("NEXDASH_SECRET_KEY")
        or _DEFAULT_MASTER_KEY
    )
    key_bytes = raw_key.encode("utf-8") if isinstance(raw_key, str) else bytes(raw_key)
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(key_bytes)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secrets(
    secrets_map: dict[str, str] | None,
    *,
    secret_key: str | bytes | None = None,
) -> str:
    """Encrypt a dictionary of secrets into a URL-safe Fernet token string."""
    if not secrets_map:
        return ""
    normalized = normalize_secrets(secrets_map)
    if not normalized:
        return ""
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _build_fernet(secret_key).encrypt(payload).decode("ascii")


def decrypt_secrets(
    ciphertext: str | None,
    *,
    secret_key: str | bytes | None = None,
) -> dict[str, str]:
    """Decrypt a Fernet token string back into a secrets dictionary."""
    if not ciphertext:
        return {}
    try:
        raw = _build_fernet(secret_key).decrypt(ciphertext.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise SourceValidationError("Decrypted secrets payload is not a JSON object.")
        return {str(k): str(v) for k, v in parsed.items()}
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise SourceValidationError(
            "Stored secrets cannot be decrypted (the server encryption key may have changed)."
        ) from exc


def normalize_secrets(secrets_map: dict[str, Any] | None) -> dict[str, str]:
    """Validate secret names and coerce values to strings."""
    result: dict[str, str] = {}
    for raw_name, raw_val in (secrets_map or {}).items():
        name = str(raw_name).strip()
        if not SECRET_NAME_RE.match(name):
            raise SourceValidationError(
                f"Invalid secret name '{name}': use ASCII letters, digits, and underscores."
            )
        result[name] = "" if raw_val is None else str(raw_val)
    if len(result) > MAX_SECRETS:
        raise SourceValidationError(f"Maximum of {MAX_SECRETS} secrets per dashboard exceeded.")
    return result


def merge_secrets(
    current: dict[str, str] | None,
    updates: dict[str, Any] | None,
) -> dict[str, str]:
    """Merge secret updates into existing secrets. Empty string removes a secret."""
    merged = dict(current or {})
    for name, value in normalize_secrets(updates).items():
        if value == "":
            merged.pop(name, None)
        else:
            merged[name] = value
    if len(merged) > MAX_SECRETS:
        raise SourceValidationError(f"Maximum of {MAX_SECRETS} secrets per dashboard exceeded.")
    return merged


def mask_secrets(text: str, secrets_map: dict[str, str] | None) -> str:
    """Replace any secret value (length >= 3) appearing in text with SECRET_MASK."""
    if not text or not secrets_map:
        return text
    values = sorted(
        (v for v in secrets_map.values() if v and len(v) >= 3),
        key=len,
        reverse=True,
    )
    masked = text
    for val in values:
        masked = masked.replace(val, SECRET_MASK)
    return masked


def substitute_secrets(value: Any, secrets_map: dict[str, str]) -> Any:
    """Recursively substitute {{env.NAME}} placeholders using secrets_map."""
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in secrets_map:
                raise SourceValidationError(f"Missing required secret '{key}'.")
            return secrets_map[key]

        return ENV_REF_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: substitute_secrets(v, secrets_map) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_secrets(item, secrets_map) for item in value]
    return value


def extract_secret_refs(recipe_or_sources: Any) -> set[str]:
    """Return the set of secret names referenced via {{env.NAME}} in a recipe or source list."""
    if isinstance(recipe_or_sources, dict) and "sources" in recipe_or_sources:
        target = recipe_or_sources.get("sources") or []
    else:
        target = recipe_or_sources
    serialized = json.dumps(target, ensure_ascii=False, default=str)
    return set(ENV_REF_RE.findall(serialized))


def validate_secret_refs(recipe_or_sources: Any, secrets_map: dict[str, str]) -> None:
    """Ensure every {{env.NAME}} placeholder referenced in the recipe is present in secrets_map."""
    missing = sorted(extract_secret_refs(recipe_or_sources) - set(secrets_map))
    if missing:
        raise SourceValidationError("Missing required secrets: " + ", ".join(missing) + ".")


def _require_env_placeholder(value: Any, location: str) -> None:
    """Reject literal credentials; sensitive fields must reference {{env.NAME}}."""
    if value not in (None, "") and not ENV_REF_RE.search(str(value)):
        raise SourceValidationError(
            f"{location} looks like a credential: store its value in secrets and reference {{{{env.NAME}}}}."
        )


# ---------------------------------------------------------------------------
# SSRF and SQL Guards
# ---------------------------------------------------------------------------


def validate_url(url: str | None) -> str:
    """Validate URL syntax, scheme (http/https), host presence, and absence of inline credentials."""
    cleaned = (url or "").strip()
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceValidationError("Source URL must start with http:// or https:// and include a hostname.")
    if parts.username or parts.password:
        raise SourceValidationError(
            "Do not embed username or password in the URL; use a header with {{env.NAME}}."
        )
    return cleaned


def _check_ip_allowed(ip_str: str, host: str, *, allow_private: bool = False) -> None:
    """Check whether a single IP address is safe to connect to under SSRF rules."""
    ip = ipaddress.ip_address(ip_str)
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        raise SSRFError(f"Host '{host}' resolves to a restricted address ({ip_str}).")
    if not allow_private and ip.is_private:
        raise SSRFError(f"Host '{host}' resolves to a private network address ({ip_str}).")


def check_ssrf_host(
    host: str,
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
) -> list[str]:
    """Validate that a hostname does not point to loopback, metadata/link-local, or internal hosts.

    Args:
        host: Hostname or IP literal from the URL.
        allow_private: Whether RFC1918 private addresses are permitted (loopback and
            link-local 169.254.x.x are ALWAYS blocked regardless of this flag).
        resolver: Optional custom hostname resolver returning a list of IP strings
            (used for deterministic offline tests).

    Returns:
        List of validated IP addresses for the host.

    Raises:
        SSRFError: If the host is a single-word internal name, fails resolution, or
            resolves to a blocked IP range.
    """
    cleaned = (host or "").strip().strip("[]")
    if not cleaned:
        raise SSRFError("Empty hostname is not allowed.")

    # Direct IP literal check before DNS lookup
    try:
        ipaddress.ip_address(cleaned)
        _check_ip_allowed(cleaned, cleaned, allow_private=allow_private)
        return [cleaned]
    except ValueError:
        pass

    # Single-label hostnames (e.g. localhost, postgres, internal_api) are blocked
    if "." not in cleaned and ":" not in cleaned:
        raise SSRFError(
            f"Single-label host '{cleaned}' is not allowed; use a fully qualified domain name."
        )

    if resolver is not None:
        resolved_ips = resolver(cleaned)
    else:
        try:
            infos = socket.getaddrinfo(cleaned, None)
            resolved_ips = [str(info[4][0]) for info in infos]
        except socket.gaierror as exc:
            raise SSRFError(f"Host '{cleaned}' could not be resolved ({exc}).") from exc

    if not resolved_ips:
        raise SSRFError(f"Host '{cleaned}' did not resolve to any IP address.")

    for ip_str in resolved_ips:
        _check_ip_allowed(ip_str, cleaned, allow_private=allow_private)

    return resolved_ips


def validate_select_sql(sql: str | None) -> str:
    """Enforce that a SQL query is a single read-only SELECT or WITH ... SELECT statement.

    Strips SQL comments and single-quoted string literals before checking for:
    - Leading SELECT or WITH keyword
    - Absence of semicolon statement separators
    - Absence of DDL/DML/administrative keywords and xp_*/sp_* procedures
    """
    cleaned = (sql or "").strip().rstrip(";").strip()
    if not cleaned:
        raise SQLGuardError("SQL query cannot be empty.")

    # Strip line and block comments
    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", " ", cleaned, flags=re.DOTALL)
    # Neutralize single-quoted string literals so words inside strings do not false-positive
    without_literals = re.sub(r"'(?:[^']|'')*'", "''", without_comments)

    if not re.match(r"^\s*(select|with)\b", without_literals, re.IGNORECASE):
        raise SQLGuardError("SQL source only accepts read-only queries starting with SELECT or WITH.")

    if ";" in without_literals:
        raise SQLGuardError("Only a single SQL statement is allowed per source (no ';' separator).")

    forbidden = _FORBIDDEN_SQL_RE.search(without_literals)
    if forbidden:
        raise SQLGuardError(
            f"SQL source is read-only: forbidden keyword or procedure '{forbidden.group(0)}' detected."
        )

    return cleaned


# ---------------------------------------------------------------------------
# Recipe & Source Normalization
# ---------------------------------------------------------------------------


def normalize_source(source: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a single data source definition into canonical form."""
    if not isinstance(source, dict):
        raise SourceValidationError("Each source definition must be a dictionary.")

    name = str(source.get("name") or "").strip()
    if not SOURCE_NAME_RE.match(name):
        raise SourceValidationError(
            f"Invalid source name '{name}': use lowercase letters, digits, and underscores "
            f"(becomes window.ASKDASH_DATA.{name or 'source'})."
        )

    kind = str(source.get("kind") or "").strip().lower()
    if kind not in SOURCE_KINDS:
        raise SourceValidationError(
            f"Invalid source kind '{kind}' in '{name}': must be one of {', '.join(SOURCE_KINDS)}."
        )

    if kind == "http":
        method = str(source.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            raise SourceValidationError(f"Source '{name}': unsupported HTTP method '{method}' (use GET or POST).")
        headers = {
            str(k).strip(): str(v)
            for k, v in (source.get("headers") or {}).items()
            if str(k).strip()
        }
        for k, v in headers.items():
            if _SENSITIVE_KEY_RE.search(k):
                _require_env_placeholder(v, f"Source '{name}' header '{k}'")

        params = {str(k).strip(): str(v) for k, v in (source.get("params") or {}).items() if str(k).strip()}
        for k, v in params.items():
            if _SENSITIVE_KEY_RE.search(k):
                _require_env_placeholder(v, f"Source '{name}' param '{k}'")

        url = validate_url(source.get("url"))
        for k, v in parse_qsl(urlsplit(url).query):
            if _SENSITIVE_KEY_RE.search(k):
                _require_env_placeholder(v, f"Source '{name}' URL query param '{k}'")

        return {
            "name": name,
            "kind": "http",
            "method": method,
            "url": url,
            "headers": headers,
            "params": params,
            "body": source.get("body") if method == "POST" else None,
            "json_path": str(source.get("json_path") or "").strip(),
        }

    if kind == "sql":
        raw_plan = source.get("plan")
        raw_sql = source.get("sql")
        if not raw_plan and not raw_sql:
            raise SourceValidationError(
                f"Source '{name}': SQL source requires either a parameterized 'sql' query or a semantic 'plan'."
            )

        validated_sql = validate_select_sql(raw_sql) if raw_sql else ""
        if raw_plan is not None:
            plan_obj = QueryPlan.model_validate(raw_plan) if isinstance(raw_plan, dict) else raw_plan
            if not isinstance(plan_obj, QueryPlan):
                raise SourceValidationError(f"Source '{name}': invalid semantic QueryPlan.")
            normalized_plan: dict[str, Any] | None = plan_obj.model_dump(mode="json")
        else:
            normalized_plan = None

        raw_params = dict(source.get("params") or {})
        for k, v in raw_params.items():
            if not SECRET_NAME_RE.match(str(k)):
                raise SourceValidationError(f"Source '{name}': invalid SQL parameter name '{k}'.")
            if isinstance(v, (dict, list)):
                raise SourceValidationError(
                    f"Source '{name}': SQL parameter '{k}' must be a scalar value."
                )

        conn = dict(source.get("connection") or {})
        if conn:
            if "password" in conn and conn["password"] not in (None, ""):
                _require_env_placeholder(conn["password"], f"Source '{name}' connection password")
            if "dsn" in conn and conn["dsn"] not in (None, "") and "://" in str(conn["dsn"]):
                parts = urlsplit(str(conn["dsn"]))
                if parts.password:
                    _require_env_placeholder(conn["dsn"], f"Source '{name}' connection DSN")

        max_rows = max(1, min(int(source.get("max_rows") or DEFAULT_MAX_ROWS), MAX_ROWS_LIMIT))
        return {
            "name": name,
            "kind": "sql",
            "sql": validated_sql,
            "params": raw_params,
            "plan": normalized_plan,
            "connection": conn,
            "max_rows": max_rows,
        }

    if kind == "csv":
        raw_url = source.get("url")
        raw_content = source.get("content")
        if not raw_url and raw_content is None:
            raise SourceValidationError(
                f"Source '{name}': CSV source requires either 'url' or inline 'content'."
            )
        url = validate_url(raw_url) if raw_url else ""
        headers = {
            str(k).strip(): str(v)
            for k, v in (source.get("headers") or {}).items()
            if str(k).strip()
        }
        for k, v in headers.items():
            if _SENSITIVE_KEY_RE.search(k):
                _require_env_placeholder(v, f"Source '{name}' header '{k}'")

        for k, v in parse_qsl(urlsplit(url).query) if url else []:
            if _SENSITIVE_KEY_RE.search(k):
                _require_env_placeholder(v, f"Source '{name}' URL query param '{k}'")

        return {
            "name": name,
            "kind": "csv",
            "url": url,
            "content": str(raw_content) if raw_content is not None else None,
            "headers": headers,
            "header_row": max(1, int(source.get("header_row") or 1)),
            "max_rows": max(1, min(int(source.get("max_rows") or DEFAULT_MAX_ROWS), MAX_ROWS_LIMIT)),
        }

    # kind == "sheets"
    url = validate_url(source.get("url"))
    if "docs.google.com/spreadsheets/" in url and not _GOOGLE_SHEETS_ID_RE.search(url):
        raise SourceValidationError(
            f"Source '{name}': Google Sheets URL is missing the spreadsheet ID (/spreadsheets/d/<id>)."
        )
    headers = {
        str(k).strip(): str(v)
        for k, v in (source.get("headers") or {}).items()
        if str(k).strip()
    }
    for k, v in headers.items():
        if _SENSITIVE_KEY_RE.search(k):
            _require_env_placeholder(v, f"Source '{name}' header '{k}'")

    sheet_tab = str(source.get("sheet") or source.get("tab") or "").strip()
    return {
        "name": name,
        "kind": "sheets",
        "url": url,
        "sheet": sheet_tab,
        "headers": headers,
        "header_row": max(1, int(source.get("header_row") or 1)),
        "max_rows": max(1, min(int(source.get("max_rows") or DEFAULT_MAX_ROWS), MAX_ROWS_LIMIT)),
    }


def normalize_recipe(recipe: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any]:
    """Validate and normalize a dashboard data recipe containing 1..MAX_SOURCES sources."""
    if isinstance(recipe, list):
        raw_sources = recipe
    elif isinstance(recipe, dict):
        raw_sources = list(recipe.get("sources") or [])
    else:
        raw_sources = []

    if not raw_sources:
        raise SourceValidationError("At least one data source must be provided.")
    if len(raw_sources) > MAX_SOURCES:
        raise SourceValidationError(f"Maximum of {MAX_SOURCES} data sources per dashboard exceeded.")

    normalized = [normalize_source(dict(s)) for s in raw_sources]
    names = [s["name"] for s in normalized]
    if len(set(names)) != len(names):
        raise SourceValidationError("Duplicate data source names are not allowed.")

    return {"type": "sources", "sources": normalized}


def summarize_sources(recipe: dict[str, Any] | None) -> str:
    """Return a short human-readable summary of a recipe's sources."""
    sources = (recipe or {}).get("sources") or []
    return ", ".join(f"{s['name']} ({s['kind']})" for s in sources)


# ---------------------------------------------------------------------------
# CSV, Sheets, JSON Path, and HTTP Helpers
# ---------------------------------------------------------------------------


def extract_json_path(data: Any, path: str) -> Any:
    """Navigate a dot-separated path (e.g. 'data.items.0') inside parsed JSON."""
    for part in [p for p in path.split(".") if p]:
        if isinstance(data, list) and part.lstrip("-").isdigit():
            idx = int(part)
            try:
                data = data[idx]
            except IndexError as exc:
                raise SourceCollectionError(
                    f"json_path '{path}' index '{part}' out of range."
                ) from exc
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise SourceCollectionError(
                f"json_path '{path}' not found in response (stopped at '{part}')."
            )
    return data


def google_sheets_csv_url(url: str, sheet: str = "") -> str:
    """Convert a Google Sheets sharing/edit link into a direct CSV export URL.

    Already-published links (/pub?output=csv) or non-Google CSV links are returned as-is.
    """
    parts = urlsplit(url)
    if "/pub" in parts.path:
        return url
    match = _GOOGLE_SHEETS_ID_RE.search(url)
    if not match:
        return url
    sheet_id = match.group(1)
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    if sheet:
        return f"{base}/gviz/tq?tqx=out:csv&sheet={quote(sheet)}"
    gid_match = re.search(r"[#&?]gid=(\d+)", url)
    gid_suffix = f"&gid={gid_match.group(1)}" if gid_match else ""
    return f"{base}/export?format=csv{gid_suffix}"


def _parse_cell_number(val: str) -> Any:
    """Convert standard ('1234.5') and Brazilian ('1.234,56') numbers while keeping leading-zero codes."""
    text = val.strip()
    if _NUMBER_STD_RE.match(text):
        return int(text) if "." not in text else float(text)
    if _NUMBER_BR_RE.match(text):
        return float(text.replace(".", "").replace(",", "."))
    return val


def _json_safe_value(val: Any) -> Any:
    """Convert dates, Decimals, UUIDs, and binary values into JSON-serializable primitives."""
    if isinstance(val, (dt.datetime, dt.date, dt.time)):
        return val.isoformat()
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, (bytes, bytearray)):
        return base64.b64encode(bytes(val)).decode("ascii")
    return val


def _deduplicate_headers(row: list[Any]) -> list[str]:
    """Generate unique non-empty column names from a header row."""
    names: list[str] = []
    for idx, cell in enumerate(row):
        base_name = str(cell).strip() if cell not in (None, "") else f"col_{idx + 1}"
        candidate = base_name
        suffix = 2
        while candidate in names:
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        names.append(candidate)
    return names


def parse_csv_bytes(
    raw_bytes: bytes | str,
    *,
    header_row: int = 1,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Parse CSV bytes or string into a list of dictionaries with type coercion."""
    if isinstance(raw_bytes, str):
        text = raw_bytes
    else:
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw_bytes.decode("cp1252", errors="replace")

    if not text.strip():
        return []

    try:
        dialect: Any = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    all_rows = [
        [_parse_cell_number(cell) for cell in line]
        for line in csv.reader(io.StringIO(text), dialect)
    ]
    sliced = all_rows[max(0, header_row - 1) :]
    if not sliced:
        return []

    headers = _deduplicate_headers(sliced[0])
    records: list[dict[str, Any]] = []
    for row in sliced[1:]:
        if all(cell in (None, "") for cell in row):
            continue
        padded = list(row) + [None] * max(0, len(headers) - len(row))
        records.append({col: _json_safe_value(val) for col, val in zip(headers, padded, strict=False)})
        if len(records) >= max_rows:
            break
    return records


# ---------------------------------------------------------------------------
# Guarded HTTP Fetch & Source Execution
# ---------------------------------------------------------------------------


def fetch_url_guarded(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    http_fetcher: HttpFetcherFn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> HttpFetchResult:
    """Fetch an HTTP/HTTPS URL following redirects manually and checking SSRF on every hop.

    Sensitive headers (Authorization, Cookie, API keys) are stripped if a redirect crosses
    to a different hostname.
    """
    current_method = method.upper()
    current_url = url
    current_headers = dict(headers or {})
    current_body = body

    if http_fetcher is not None:
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(current_url).hostname or ""
            check_ssrf_host(host, allow_private=allow_private, resolver=resolver)
            result = http_fetcher(current_method, current_url, current_headers, current_body)
            resp_headers = {k.lower(): v for k, v in (result.headers or {}).items()}
            if result.status_code in (301, 302, 303, 307, 308) and "location" in resp_headers:
                target_url = urljoin(current_url, resp_headers["location"])
                validate_url(target_url)
                if urlsplit(target_url).hostname != urlsplit(current_url).hostname:
                    current_headers = {
                        k: v for k, v in current_headers.items() if not _SENSITIVE_KEY_RE.search(k)
                    }
                current_url = target_url
                if result.status_code == 303:
                    current_method, current_body = "GET", None
                continue
            if result.status_code >= 300:
                raise SourceCollectionError(f"HTTP {result.status_code}")
            if len(result.content) > MAX_RESPONSE_BYTES:
                raise SourceCollectionError("Response exceeded maximum size of 8 MB.")
            return result
        raise SourceCollectionError(f"Exceeded maximum of {MAX_REDIRECTS} redirects.")

    with httpx.Client(
        timeout=HTTP_TIMEOUT_S,
        follow_redirects=False,
        transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(current_url).hostname or ""
            check_ssrf_host(host, allow_private=allow_private, resolver=resolver)
            json_payload = current_body if isinstance(current_body, (dict, list)) else None
            content_payload = current_body if isinstance(current_body, (str, bytes)) else None
            with client.stream(
                current_method,
                current_url,
                headers=current_headers,
                json=json_payload,
                content=content_payload,
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    target_url = urljoin(current_url, resp.headers["location"])
                    validate_url(target_url)
                    if urlsplit(target_url).hostname != urlsplit(current_url).hostname:
                        current_headers = {
                            k: v
                            for k, v in current_headers.items()
                            if not _SENSITIVE_KEY_RE.search(k)
                        }
                    current_url = target_url
                    if resp.status_code == 303:
                        current_method, current_body = "GET", None
                    continue

                if resp.status_code >= 300:
                    raise SourceCollectionError(f"HTTP {resp.status_code}")

                buffer = bytearray()
                for chunk in resp.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_RESPONSE_BYTES:
                        raise SourceCollectionError("Response exceeded maximum size of 8 MB.")

                return HttpFetchResult(
                    content=bytes(buffer),
                    content_type=resp.headers.get("content-type", ""),
                    encoding=resp.encoding or "utf-8",
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                )

    raise SourceCollectionError(f"Exceeded maximum of {MAX_REDIRECTS} redirects.")


def _collect_http_source(
    source: dict[str, Any],
    secrets_map: dict[str, str],
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    http_fetcher: HttpFetcherFn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    url = substitute_secrets(source["url"], secrets_map)
    params = substitute_secrets(source.get("params") or {}, secrets_map)
    if params:
        parts = urlsplit(url)
        merged_query = "&".join(q for q in (parts.query, urlencode(params)) if q)
        url = urlunsplit(parts._replace(query=merged_query))

    headers = substitute_secrets(source.get("headers") or {}, secrets_map)
    body = substitute_secrets(source.get("body"), secrets_map)

    fetched = fetch_url_guarded(
        source["method"],
        url,
        headers=headers,
        body=body,
        allow_private=allow_private,
        resolver=resolver,
        http_fetcher=http_fetcher,
        transport=transport,
    )
    text = fetched.content.decode(fetched.encoding or "utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        if source.get("json_path"):
            raise SourceCollectionError(
                "Response is not valid JSON; cannot apply json_path."
            ) from exc
        return text

    json_path = source.get("json_path") or ""
    return extract_json_path(parsed, json_path) if json_path else parsed


def _collect_csv_source(
    source: dict[str, Any],
    secrets_map: dict[str, str],
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    http_fetcher: HttpFetcherFn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    if source.get("content") is not None and not source.get("url"):
        raw_content = substitute_secrets(source["content"], secrets_map)
        return parse_csv_bytes(
            raw_content,
            header_row=source.get("header_row", 1),
            max_rows=source.get("max_rows", DEFAULT_MAX_ROWS),
        )

    url = substitute_secrets(source["url"], secrets_map)
    headers = substitute_secrets(source.get("headers") or {}, secrets_map)
    fetched = fetch_url_guarded(
        "GET",
        url,
        headers=headers,
        allow_private=allow_private,
        resolver=resolver,
        http_fetcher=http_fetcher,
        transport=transport,
    )
    return parse_csv_bytes(
        fetched.content,
        header_row=source.get("header_row", 1),
        max_rows=source.get("max_rows", DEFAULT_MAX_ROWS),
    )


def _collect_sheets_source(
    source: dict[str, Any],
    secrets_map: dict[str, str],
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    http_fetcher: HttpFetcherFn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    raw_url = substitute_secrets(source["url"], secrets_map)
    export_url = google_sheets_csv_url(raw_url, source.get("sheet") or "")
    headers = substitute_secrets(source.get("headers") or {}, secrets_map)
    fetched = fetch_url_guarded(
        "GET",
        export_url,
        headers=headers,
        allow_private=allow_private,
        resolver=resolver,
        http_fetcher=http_fetcher,
        transport=transport,
    )
    if "text/html" in (fetched.content_type or "").lower():
        raise SourceCollectionError(
            "Google Sheets returned an HTML login page; share the sheet with 'Anyone with the link' "
            "or publish it to the web as CSV."
        )
    return parse_csv_bytes(
        fetched.content,
        header_row=source.get("header_row", 1),
        max_rows=source.get("max_rows", DEFAULT_MAX_ROWS),
    )


def _collect_sql_source(
    source: dict[str, Any],
    secrets_map: dict[str, str],
    *,
    sql_executor: SQLiteExecutor | sqlite3.Connection | None = None,
    cube_registry: CubeRegistry | dict[str, Cube] | None = None,
) -> list[dict[str, Any]]:
    max_rows = int(source.get("max_rows") or DEFAULT_MAX_ROWS)

    # 1. Semantic QueryPlan execution
    if source.get("plan"):
        plan = QueryPlan.model_validate(source["plan"])
        if cube_registry is None:
            raise SourceCollectionError(
                f"SQL source '{source['name']}' uses a semantic QueryPlan, but no CubeRegistry was provided."
            )
        cube = (
            cube_registry.get(plan.cube)
            if isinstance(cube_registry, CubeRegistry)
            else cube_registry.get(plan.cube)
        )
        if cube is None:
            raise SourceCollectionError(f"Cube '{plan.cube}' not found in registry.")
        reg_obj = cube_registry if isinstance(cube_registry, CubeRegistry) else None
        if isinstance(sql_executor, SQLiteExecutor):
            exec_res = sql_executor.execute_plan(plan, cube, registry=reg_obj)
            return [{k: _json_safe_value(v) for k, v in row.items()} for row in exec_res.rows[:max_rows]]
        if isinstance(sql_executor, sqlite3.Connection):
            compiled = SQLCompiler(default_dialect="sqlite").compile(
                plan, cube, registry=reg_obj, dialect="sqlite"
            )
            return _execute_sqlite_bound(sql_executor, compiled.sql, compiled.params, max_rows)
        raise SourceCollectionError(
            f"SQL source '{source['name']}' requires a SQLiteExecutor or sqlite3.Connection."
        )

    # 2. Guarded read-only SELECT execution with bound parameters
    validated_sql = validate_select_sql(source["sql"])
    bound_params = substitute_secrets(source.get("params") or {}, secrets_map)
    conn_cfg = substitute_secrets(source.get("connection") or {}, secrets_map)

    if isinstance(sql_executor, SQLiteExecutor):
        return _execute_sqlite_bound(sql_executor.connection, validated_sql, bound_params, max_rows)
    if isinstance(sql_executor, sqlite3.Connection):
        return _execute_sqlite_bound(sql_executor, validated_sql, bound_params, max_rows)

    db_path = conn_cfg.get("database") or conn_cfg.get("path")
    if db_path:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return _execute_sqlite_bound(conn, validated_sql, bound_params, max_rows)

    raise SourceCollectionError(
        f"SQL source '{source['name']}' has no active database connection or executor."
    )


def _execute_sqlite_bound(
    connection: sqlite3.Connection,
    sql: str,
    params: dict[str, Any],
    max_rows: int,
) -> list[dict[str, Any]]:
    """Execute a validated read-only SELECT query with bound parameters on SQLite."""
    validate_select_sql(sql)
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(max_rows)
        return [
            {col: _json_safe_value(val) for col, val in zip(columns, row, strict=False)}
            for row in fetched
        ]
    finally:
        cursor.close()


def collect_single_source(
    source: dict[str, Any],
    secrets_map: dict[str, str] | None = None,
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    http_fetcher: HttpFetcherFn | None = None,
    transport: httpx.BaseTransport | None = None,
    sql_executor: SQLiteExecutor | sqlite3.Connection | None = None,
    cube_registry: CubeRegistry | dict[str, Cube] | None = None,
) -> Any:
    """Collect data from a single normalized source definition."""
    normalized = normalize_source(source)
    sec = normalize_secrets(secrets_map)
    validate_secret_refs([normalized], sec)

    kind = normalized["kind"]
    if kind == "http":
        return _collect_http_source(
            normalized,
            sec,
            allow_private=allow_private,
            resolver=resolver,
            http_fetcher=http_fetcher,
            transport=transport,
        )
    if kind == "csv":
        return _collect_csv_source(
            normalized,
            sec,
            allow_private=allow_private,
            resolver=resolver,
            http_fetcher=http_fetcher,
            transport=transport,
        )
    if kind == "sheets":
        return _collect_sheets_source(
            normalized,
            sec,
            allow_private=allow_private,
            resolver=resolver,
            http_fetcher=http_fetcher,
            transport=transport,
        )
    if kind == "sql":
        return _collect_sql_source(
            normalized,
            sec,
            sql_executor=sql_executor,
            cube_registry=cube_registry,
        )
    raise SourceValidationError(f"Unsupported source kind '{kind}'.")


def _try_collect_source(
    source: dict[str, Any],
    secrets_map: dict[str, str],
    **kwargs: Any,
) -> tuple[bool, Any]:
    try:
        data = collect_single_source(source, secrets_map, **kwargs)
        return True, data
    except Exception as exc:
        msg = str(exc) if isinstance(exc, SourceError) else f"{type(exc).__name__}: {exc}"
        return False, mask_secrets(msg, secrets_map)[:300]


def collect_recipe_data(
    recipe: dict[str, Any] | list[dict[str, Any]],
    secrets_map: dict[str, str] | None = None,
    *,
    now: dt.datetime | None = None,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    http_fetcher: HttpFetcherFn | None = None,
    transport: httpx.BaseTransport | None = None,
    sql_executor: SQLiteExecutor | sqlite3.Connection | None = None,
    cube_registry: CubeRegistry | dict[str, Cube] | None = None,
) -> dict[str, Any]:
    """Collect all sources in a recipe atomically (all-or-nothing).

    If any source fails, raises SourceCollectionError with all secret values masked,
    allowing the caller (refresh_dashboard) to keep the previous dataset intact.
    """
    normalized_recipe = normalize_recipe(recipe)
    sec = normalize_secrets(secrets_map)
    validate_secret_refs(normalized_recipe, sec)

    sources = normalized_recipe["sources"]
    results = [
        _try_collect_source(
            src,
            sec,
            allow_private=allow_private,
            resolver=resolver,
            http_fetcher=http_fetcher,
            transport=transport,
            sql_executor=sql_executor,
            cube_registry=cube_registry,
        )
        for src in sources
    ]

    errors = [
        f"source '{src['name']}': {res}"
        for src, (ok, res) in zip(sources, results, strict=False)
        if not ok
    ]
    if errors:
        raise SourceCollectionError("; ".join(errors))

    data_by_name = {src["name"]: res for src, (_, res) in zip(sources, results, strict=False)}
    timestamp = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat()
    row_counts = {
        name: (len(val) if isinstance(val, list) else 1)
        for name, val in data_by_name.items()
    }
    return {
        "type": "sources",
        "sources": data_by_name,
        "collected_at": timestamp,
        "rows": row_counts,
    }


def preview_recipe_sources(
    recipe: dict[str, Any] | list[dict[str, Any]],
    secrets_map: dict[str, str] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Test each source in a recipe without persisting, returning row counts and sample rows."""
    normalized_recipe = normalize_recipe(recipe)
    sec = normalize_secrets(secrets_map)
    validate_secret_refs(normalized_recipe, sec)

    previews: list[dict[str, Any]] = []
    for src in normalized_recipe["sources"]:
        ok, res = _try_collect_source(src, sec, **kwargs)
        item: dict[str, Any] = {"name": src["name"], "kind": src["kind"], "ok": ok}
        if ok:
            item["rows"] = len(res) if isinstance(res, list) else 1
            item["sample"] = res[:PREVIEW_SAMPLE_ROWS] if isinstance(res, list) else res
        else:
            item["error"] = res
        previews.append(item)
    return previews
