"""Live dashboards: agent-written HTML with server-injected data, CSP, cadence, expiry, and edit tokens.

Lifecycle:
    publish -> active --(D-3: warn_d3)--(D-0: warn_d0)--> expired --(+60d)--> removed
                 ^                                           |
                 +-------------- renew (+30 days) <----------+
    permanent: never expires; emits warn_idle once after 90 days without access.

Security properties:
- Rendered pages are served under a strict Content-Security-Policy with `connect-src 'none'`.
  The browser never sees credentials and cannot make outbound network requests.
- Data collected on the server is injected into `<head>` as `window.NEXDASH_DATA`
  before any agent script runs, with `<`, `>`, `&`, U+2028, and
  U+2029 escaped inside `<script>` to prevent script-breakout XSS.
- Viewing uses an opaque `access_token`; renewal uses a separate `renewal_token` (POST);
  editing uses a one-time `edit_token` (`ed_...`) stored only as a SHA-256 hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from nexdash.cube import Cube, CubeRegistry
from nexdash.executor import SQLiteExecutor
from nexdash.sources import (
     MAX_RESPONSE_BYTES,
    TIME_HHMM_RE,
    HttpFetcherFn,
    ResolverFn,
    SourceCollectionError,
    SourceValidationError,
    collect_recipe_data,
    decrypt_secrets,
    encrypt_secrets,
    mask_secrets,
    merge_secrets,
    normalize_recipe,
    normalize_secrets,
    preview_recipe_sources,
    summarize_sources,
    validate_secret_refs,
)

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REMOVED = "removed"

CADENCES = ("never", "hourly", "daily", "weekly")

VALIDITY_DAYS = 30
WARNING_DAYS_BEFORE = 3
GRACE_PERIOD_DAYS = 60
MAX_VALIDITY_DAYS = 2 * VALIDITY_DAYS
IDLE_DAYS = 90
REFRESH_COOLDOWN_MINUTES = 15
DEFAULT_REFRESH_TIME = "06:30"
DEFAULT_TZ_OFFSET_HOURS = -3
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_NOTES = 10
MAX_NOTE_CHARS = 1000
MAX_HISTORY = 50

DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


class DashboardError(Exception):
    """Base exception for dashboard lifecycle operations."""


class DashboardValidationError(DashboardError, ValueError):
    """Raised when dashboard publication or edit parameters are invalid."""


class DashboardNotRefreshableError(DashboardError, ValueError):
    """Raised when attempting to refresh a static snapshot or inactive dashboard."""


class DashboardNotRenewableError(DashboardError, ValueError):
    """Raised when attempting to renew or edit a dashboard whose content was removed."""


class DashboardAuthError(DashboardError, PermissionError):
    """Raised when an edit or renewal token is invalid."""


# ---------------------------------------------------------------------------
# Pure Time, Cadence, and Expiry Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_iso_datetime(iso_str: str) -> dt.datetime:
    """Parse an ISO-8601 datetime string, defaulting naive timestamps to UTC."""
    parsed = dt.datetime.fromisoformat(iso_str)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def validate_refresh_time(refresh_time: str | None) -> str:
    """Validate an HH:MM 24-hour clock string (defaulting to 06:30)."""
    cleaned = (refresh_time or "").strip() or DEFAULT_REFRESH_TIME
    if not TIME_HHMM_RE.match(cleaned):
        raise DashboardValidationError(f"Invalid refresh time '{cleaned}' (expected HH:MM).")
    return cleaned


def next_refresh_at(
    cadence: str,
    now: dt.datetime,
    refresh_time: str = DEFAULT_REFRESH_TIME,
    tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS,
) -> str:
    """Compute the next scheduled refresh timestamp in UTC ISO-8601 format.

    - 'never': returns ''
    - 'hourly': start of the next full hour
    - 'daily': next occurrence of `refresh_time` in target timezone
    - 'weekly': next Monday at `refresh_time` in target timezone
    """
    if cadence == "hourly":
        target = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        return target.isoformat()
    if cadence not in ("daily", "weekly"):
        return ""

    valid_time = validate_refresh_time(refresh_time)
    hour, minute = (int(part) for part in valid_time.split(":"))
    tz = dt.timezone(dt.timedelta(hours=tz_offset_hours))
    local_now = now.astimezone(tz)
    target_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_local <= local_now:
        target_local += dt.timedelta(days=1)
    if cadence == "weekly":
        days_until_monday = (7 - target_local.weekday()) % 7
        target_local += dt.timedelta(days=days_until_monday)
    return target_local.astimezone(dt.UTC).isoformat()


def can_refresh_now(
    dashboard: PublishedDashboard,
    now: dt.datetime,
    cooldown_minutes: int = REFRESH_COOLDOWN_MINUTES,
) -> tuple[bool, int]:
    """Return (allowed, seconds_remaining) for manual refresh rate limiting."""
    if not dashboard.updated_at:
        return True, 0
    elapsed = (now - parse_iso_datetime(dashboard.updated_at)).total_seconds()
    remaining = cooldown_minutes * 60 - elapsed
    return (remaining <= 0), max(0, int(remaining))


def compute_new_expiry(
    current_expires_at: str,
    now: dt.datetime,
    validity_days: int = VALIDITY_DAYS,
    max_validity_days: int = MAX_VALIDITY_DAYS,
) -> dt.datetime:
    """Extend expiry by +validity_days from max(current_expires_at, now), capped at now + max_validity_days."""
    current = parse_iso_datetime(current_expires_at) if current_expires_at else now
    candidate = max(current, now) + dt.timedelta(days=validity_days)
    cap = now + dt.timedelta(days=max_validity_days)
    return min(candidate, cap)


def days_remaining(dashboard: PublishedDashboard, now: dt.datetime) -> int | None:
    """Return remaining whole days until expiry (rounded up; 0 if expired; None if permanent)."""
    if dashboard.permanent:
        return None
    if not dashboard.expires_at:
        return None
    try:
        delta = parse_iso_datetime(dashboard.expires_at) - now
    except ValueError:
        return None
    if delta.total_seconds() <= 0:
        return 0
    return max(0, delta.days + (1 if delta.seconds or delta.microseconds else 0))


def expiry_actions(dashboard: PublishedDashboard, now: dt.datetime) -> list[str]:
    """Determine lifecycle actions due for a dashboard at `now` (pure function).

    Possible actions: 'warn_d3', 'warn_d0', 'expire', 'remove', 'warn_idle'.
    - D-0 suppresses D-3 on the same tick to avoid duplicate notifications.
    - Expired dashboards are removed after GRACE_PERIOD_DAYS.
    - Permanent dashboards never expire; they emit 'warn_idle' once after IDLE_DAYS without access.
    """
    if dashboard.permanent:
        if dashboard.status != STATUS_ACTIVE or dashboard.idle_warned_at:
            return []
        ref_str = dashboard.last_accessed_at or dashboard.created_at
        try:
            idle_delta = now - parse_iso_datetime(ref_str)
        except ValueError:
            return []
        return ["warn_idle"] if idle_delta >= dt.timedelta(days=IDLE_DAYS) else []

    if not dashboard.expires_at:
        return []
    try:
        expires = parse_iso_datetime(dashboard.expires_at)
    except ValueError:
        return []

    remaining = expires - now

    if dashboard.status == STATUS_EXPIRED:
        if now >= expires + dt.timedelta(days=GRACE_PERIOD_DAYS):
            return ["remove"]
        return []

    if dashboard.status != STATUS_ACTIVE:
        return []

    actions: list[str] = []
    if remaining <= dt.timedelta(days=1):
        if "d0" not in dashboard.warnings_sent:
            actions.append("warn_d0")
    elif remaining <= dt.timedelta(days=WARNING_DAYS_BEFORE) and "d3" not in dashboard.warnings_sent:
        actions.append("warn_d3")

    if remaining <= dt.timedelta(0):
        actions.append("expire")

    return actions


def due_for_refresh(
    dashboards: list[PublishedDashboard],
    now: dt.datetime,
) -> list[PublishedDashboard]:
    """Return active, non-expired live dashboards that are due for scheduled refresh at `now`."""
    due: list[PublishedDashboard] = []
    for dash in dashboards:
        if dash.status != STATUS_ACTIVE or not dash.recipe or dash.cadence == "never":
            continue
        if not dash.permanent and dash.expires_at and parse_iso_datetime(dash.expires_at) <= now:
            continue
        if not dash.next_refresh_at or parse_iso_datetime(dash.next_refresh_at) <= now:
            due.append(dash)
    return due


# ---------------------------------------------------------------------------
# Edit Tokens
# ---------------------------------------------------------------------------


def hash_edit_token(token: str) -> str:
    """Compute the SHA-256 hex digest of an edit token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_edit_token() -> tuple[str, str]:
    """Generate a new (raw_edit_token, sha256_hash) pair."""
    raw = "ed_" + secrets.token_urlsafe(24)
    return raw, hash_edit_token(raw)


def verify_edit_token(token: str | None, expected_hash: str | None) -> bool:
    """Verify an edit token against its stored SHA-256 hash in constant time."""
    cleaned = (token or "").strip()
    if not cleaned.startswith("ed_") or not expected_hash:
        return False
    actual_hash = hash_edit_token(cleaned)
    return hmac.compare_digest(actual_hash, expected_hash)


# ---------------------------------------------------------------------------
# CSP & HTML Data Injection
# ---------------------------------------------------------------------------


def build_csp_headers(extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    """Return HTTP response headers enforcing CSP with connect-src 'none'."""
    headers = {
        "Content-Security-Policy": DASHBOARD_CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def safe_json_for_script(value: Any) -> str:
    """Serialize value to JSON safe for embedding inside an HTML <script> block.

    Escapes '<', '>', '&', U+2028, and U+2029 so strings like '</script>' or '<!--'
    cannot break out of the script element.
    """
    raw = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _status_footer_html() -> str:
    """Return the discrete footer bar displaying data freshness and a POST refresh button."""
    return (
        '\n<div id="nexdash-status-bar" style="position:fixed;right:12px;bottom:12px;'
        "z-index:2147483000;font:12px system-ui,sans-serif;background:rgba(23,22,21,.88);"
        'color:#F4F4F4;border-radius:8px;padding:6px 10px;display:flex;gap:8px;align-items:center"></div>\n'
        '<script>(function(){var m=window.NEXDASH_META||{},f=document.getElementById("nexdash-status-bar");'
        "if(!f)return;var d=m.updated_at?new Date(m.updated_at):null,"
        't=d?("Data as of "+d.toISOString().slice(0,16).replace("T"," ")):"";'
        'if(m.last_error)t+=" | last refresh failed";f.appendChild(document.createTextNode(t));'
        "var wait=d?((m.cooldown_minutes||15)*60000-(Date.now()-d.getTime())):0;"
        "if(m.refreshable&&wait<=0){var tok=location.pathname.split(\"/\").filter(Boolean).pop(),"
        'fm=document.createElement("form");fm.method="post";fm.action=tok+"/refresh";fm.style.margin="0";'
        'var b=document.createElement("button");b.type="submit";b.textContent="Refresh";'
        'b.style.cssText="background:#EE7203;color:#fff;border:0;border-radius:6px;padding:3px 8px;cursor:pointer";'
        "fm.appendChild(b);f.appendChild(fm);}})();</script>\n"
    )


def inject_dashboard_data(
    html: str,
    dataset: dict[str, Any] | None,
    meta: dict[str, Any] | None = None,
    *,
    inject_csp_meta: bool = True,
    inject_footer: bool = True,
) -> str:
    """Inject collected dataset, publication metadata, and CSP into agent-written HTML.

    - Injects `<meta http-equiv="Content-Security-Policy" content="...">` with `connect-src 'none'`
      (unless `inject_csp_meta=False` or a CSP meta tag is already present).
    - Injects `window.NEXDASH_DATA` and `window.NEXDASH_META` immediately after `<head>`
      before any agent script executes.
    - Injects the status/refresh footer bar immediately before `</body>`.
    """
    if isinstance(dataset, dict) and "sources" in dataset:
        sources_data = dataset.get("sources") or {}
    else:
        sources_data = dataset or {}
    collected_at = (dataset or {}).get("collected_at") if isinstance(dataset, dict) else None
    merged_meta = {
        **(meta or {}),
        "updated_at": (meta or {}).get("updated_at") or collected_at or "",
    }

    csp_tag = ""
    if inject_csp_meta and not re.search(r'http-equiv\s*=\s*["\']?Content-Security-Policy', html, re.IGNORECASE):
        csp_tag = f'<meta http-equiv="Content-Security-Policy" content="{DASHBOARD_CSP}">'

    data_json = safe_json_for_script(sources_data)
    meta_json = safe_json_for_script(merged_meta)
    script_tag = (
        f"{csp_tag}<script>"
        f"window.NEXDASH_DATA={data_json};"
        f"window.NEXDASH_META={meta_json};"
        f"</script>"
    )

    head_match = re.search(r"<head\b[^>]*>", html, re.IGNORECASE) or re.search(
        r"<html\b[^>]*>", html, re.IGNORECASE
    )
    if head_match:
        injected_html = html[: head_match.end()] + script_tag + html[head_match.end() :]
    else:
        injected_html = script_tag + html

    if not inject_footer:
        return injected_html

    footer = _status_footer_html()
    body_end = list(re.finditer(r"</body\s*>", injected_html, re.IGNORECASE))
    if body_end:
        pos = body_end[-1].start()
        return injected_html[:pos] + footer + injected_html[pos:]
    return injected_html + footer


# ---------------------------------------------------------------------------
# Dashboard Models & Store
# ---------------------------------------------------------------------------


@dataclass
class PublishedDashboard:
    """Metadata and lifecycle state of a published dashboard."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    owner_email: str = ""
    origin: str = "agent"
    status: str = STATUS_ACTIVE
    permanent: bool = False
    created_at: str = ""
    expires_at: str = ""
    updated_at: str = ""
    next_refresh_at: str = ""
    cadence: str = "never"
    refresh_time: str = DEFAULT_REFRESH_TIME
    version: int = 1
    size_bytes: int = 0
    recipe: dict[str, Any] | None = None
    encrypted_secrets: str = ""
    secret_names: list[str] = field(default_factory=list)
    access_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    renewal_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    edit_token_hash: str = ""
    edit_token_issued_at: str = ""
    consecutive_failures: int = 0
    last_error: str = ""
    access_count: int = 0
    last_accessed_at: str = ""
    idle_warned_at: str = ""
    renewal_count: int = 0
    edit_count: int = 0
    warnings_sent: list[str] = field(default_factory=list)
    notes: list[dict[str, str]] = field(default_factory=list)
    edit_history: list[dict[str, Any]] = field(default_factory=list)

    def to_public_dict(
        self,
        *,
        now: dt.datetime | None = None,
        include_renewal_url: bool = True,
        base_url: str = "/dashboards",
    ) -> dict[str, Any]:
        """Return safe public metadata without raw tokens, hashes, or secret values."""
        current_now = now or _utc_now()
        clean_base = base_url.rstrip("/")
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "owner_email": self.owner_email,
            "origin": self.origin,
            "status": self.status,
            "permanent": self.permanent,
            "kind": "live" if self.recipe else "snapshot",
            "refreshable": bool(self.recipe),
            "cadence": self.cadence,
            "refresh_time": self.refresh_time,
            "created_at": self.created_at,
            "expires_at": "" if self.permanent else self.expires_at,
            "days_remaining": days_remaining(self, current_now),
            "updated_at": self.updated_at,
            "next_refresh_at": self.next_refresh_at,
            "version": self.version,
            "size_bytes": self.size_bytes,
            "secret_names": list(self.secret_names),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "renewal_count": self.renewal_count,
            "edit_count": self.edit_count,
            "notes": list(self.notes),
            "url": f"{clean_base}/{self.access_token}",
        }
        if include_renewal_url:
            result["renewal_url"] = f"{clean_base}/renew/{self.renewal_token}"
        return result


@dataclass
class DashboardContent:
    """Stored HTML template and collected dataset for a published dashboard."""

    dashboard_id: str
    html: str = ""
    dataset: dict[str, Any] | None = None


@dataclass(frozen=True)
class RenderedDashboard:
    """Result of rendering a dashboard for viewing, including CSP headers."""

    dashboard: PublishedDashboard
    html: str
    headers: dict[str, str]


def _validate_owner_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not cleaned or "@" not in cleaned or " " in cleaned:
        raise DashboardValidationError("A valid owner_email is required for dashboard notifications.")
    return cleaned


def _validate_publication_inputs(title: str, html: str, owner_email: str) -> tuple[str, str, str]:
    clean_title = (title or "").strip()
    if not clean_title:
        raise DashboardValidationError("Dashboard title cannot be empty.")
    if not (html or "").strip():
        raise DashboardValidationError("Dashboard HTML content cannot be empty.")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise DashboardValidationError(
            f"HTML exceeds maximum size of {MAX_HTML_BYTES // (1024 * 1024)} MB."
        )
    clean_email = _validate_owner_email(owner_email)
    return clean_title[:200], html, clean_email


def _dataset_size_bytes(dataset: dict[str, Any] | None) -> int:
    if not dataset:
        return 0
    return len(json.dumps(dataset, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))


class DashboardStore:
    """Manages published dashboards, data collection, CSP rendering, renewal, and token-authenticated edits."""

    def __init__(
        self,
        *,
        secret_key: str | bytes | None = None,
        allow_private_ssrf: bool = False,
        resolver: ResolverFn | None = None,
        http_fetcher: HttpFetcherFn | None = None,
        transport: httpx.BaseTransport | None = None,
        sql_executor: SQLiteExecutor | sqlite3.Connection | None = None,
        cube_registry: CubeRegistry | dict[str, Cube] | None = None,
    ) -> None:
        self.secret_key = secret_key
        self.allow_private_ssrf = allow_private_ssrf
        self.resolver = resolver
        self.http_fetcher = http_fetcher
        self.transport = transport
        self.sql_executor = sql_executor
        self.cube_registry = cube_registry
        self._dashboards: dict[str, PublishedDashboard] = {}
        self._contents: dict[str, DashboardContent] = {}

    def _record_history(
        self,
        dashboard: PublishedDashboard,
        author: str,
        action: str,
        changes: list[str] | None = None,
        renewed: bool = False,
        now: dt.datetime | None = None,
    ) -> None:
        ts = (now or _utc_now()).astimezone(dt.UTC).isoformat()
        entry = {
            "at": ts,
            "author": (author or "unknown")[:120],
            "action": action,
            "changes": list(changes or []),
            "renewed": renewed,
        }
        dashboard.edit_history = (dashboard.edit_history + [entry])[-MAX_HISTORY:]

    def _issue_edit_token(self, dashboard: PublishedDashboard, now: dt.datetime) -> str:
        raw_token, token_hash = generate_edit_token()
        dashboard.edit_token_hash = token_hash
        dashboard.edit_token_issued_at = now.astimezone(dt.UTC).isoformat()
        return raw_token

    def _store_secrets(self, dashboard: PublishedDashboard, secrets_map: dict[str, str]) -> None:
        dashboard.encrypted_secrets = encrypt_secrets(secrets_map, secret_key=self.secret_key)
        dashboard.secret_names = sorted(secrets_map.keys())

    def _load_secrets(self, dashboard: PublishedDashboard) -> dict[str, str]:
        return decrypt_secrets(dashboard.encrypted_secrets, secret_key=self.secret_key)

    def _collect_dataset(
        self,
        recipe: dict[str, Any],
        secrets_map: dict[str, str],
        now: dt.datetime,
    ) -> dict[str, Any]:
        dataset = collect_recipe_data(
            recipe,
            secrets_map,
            now=now,
            allow_private=self.allow_private_ssrf,
            resolver=self.resolver,
            http_fetcher=self.http_fetcher,
            transport=self.transport,
            sql_executor=self.sql_executor,
            cube_registry=self.cube_registry,
        )
        if _dataset_size_bytes(dataset) > MAX_RESPONSE_BYTES:
            raise DashboardValidationError("Collected dataset exceeds maximum size of 8 MB.")
        return dataset

    def publish_snapshot(
        self,
        *,
        title: str,
        html: str,
        owner_email: str,
        description: str = "",
        origin: str = "agent",
        validity_days: int = VALIDITY_DAYS,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Publish a static agent-written HTML dashboard snapshot."""
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        clean_title, clean_html, clean_email = _validate_publication_inputs(title, html, owner_email)
        days = max(1, min(int(validity_days or VALIDITY_DAYS), VALIDITY_DAYS))

        dashboard = PublishedDashboard(
            title=clean_title,
            description=(description or "").strip()[:1000],
            owner_email=clean_email,
            origin=(origin or "agent").strip()[:80],
            created_at=current_now.isoformat(),
            expires_at=(current_now + dt.timedelta(days=days)).isoformat(),
            updated_at=current_now.isoformat(),
            size_bytes=len(clean_html.encode("utf-8")),
        )
        edit_token = self._issue_edit_token(dashboard, current_now)
        self._record_history(dashboard, origin, "publish", ["static HTML snapshot"], now=current_now)

        self._dashboards[dashboard.id] = dashboard
        self._contents[dashboard.id] = DashboardContent(dashboard_id=dashboard.id, html=clean_html)

        return {
            **dashboard.to_public_dict(now=current_now),
            "access_token": dashboard.access_token,
            "renewal_token": dashboard.renewal_token,
            "edit_token": edit_token,
        }

    def publish_live(
        self,
        *,
        title: str,
        html: str,
        sources: list[dict[str, Any]] | dict[str, Any],
        owner_email: str,
        secrets: dict[str, Any] | None = None,
        cadence: str = "never",
        refresh_time: str = DEFAULT_REFRESH_TIME,
        description: str = "",
        origin: str = "agent",
        permanent: bool = False,
        validity_days: int = VALIDITY_DAYS,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Publish a live dashboard with agent-written HTML template and server-collected data sources."""
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        clean_title, clean_html, clean_email = _validate_publication_inputs(title, html, owner_email)
        if cadence not in CADENCES:
            raise DashboardValidationError(
                f"Invalid cadence '{cadence}': must be one of {', '.join(CADENCES)}."
            )
        valid_time = validate_refresh_time(refresh_time)

        try:
            norm_secrets = normalize_secrets(secrets)
            norm_recipe = normalize_recipe(sources)
            validate_secret_refs(norm_recipe, norm_secrets)
        except SourceValidationError as exc:
            raise DashboardValidationError(str(exc)) from exc

        try:
            dataset = self._collect_dataset(norm_recipe, norm_secrets, current_now)
        except SourceCollectionError as exc:
            raise DashboardValidationError(f"Data source failed during publication: {exc}") from exc

        days = max(1, min(int(validity_days or VALIDITY_DAYS), VALIDITY_DAYS))
        dashboard = PublishedDashboard(
            title=clean_title,
            description=(description or "").strip()[:1000],
            owner_email=clean_email,
            origin=(origin or "agent").strip()[:80],
            permanent=bool(permanent),
            created_at=current_now.isoformat(),
            expires_at=(current_now + dt.timedelta(days=days)).isoformat(),
            updated_at=current_now.isoformat(),
            cadence=cadence,
            refresh_time=valid_time,
            next_refresh_at=next_refresh_at(cadence, current_now, valid_time),
            version=1,
            size_bytes=_dataset_size_bytes(dataset) + len(clean_html.encode("utf-8")),
            recipe=norm_recipe,
        )
        self._store_secrets(dashboard, norm_secrets)
        edit_token = self._issue_edit_token(dashboard, current_now)
        self._record_history(
            dashboard,
            origin,
            "publish",
            [f"sources: {summarize_sources(norm_recipe)}", f"cadence: {cadence}"],
            now=current_now,
        )

        self._dashboards[dashboard.id] = dashboard
        self._contents[dashboard.id] = DashboardContent(
            dashboard_id=dashboard.id,
            html=clean_html,
            dataset=dataset,
        )

        return {
            **dashboard.to_public_dict(now=current_now),
            "rows": dataset["rows"],
            "access_token": dashboard.access_token,
            "renewal_token": dashboard.renewal_token,
            "edit_token": edit_token,
        }

    def convert_to_live(
        self,
        dashboard_id: str,
        *,
        sources: list[dict[str, Any]] | dict[str, Any],
        secrets: dict[str, Any] | None = None,
        cadence: str = "never",
        refresh_time: str = DEFAULT_REFRESH_TIME,
        author: str = "agent",
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Upgrade a static snapshot dashboard into a live dashboard while preserving its tokens."""
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        dashboard = self.get(dashboard_id)
        if dashboard is None:
            raise DashboardValidationError(f"Dashboard '{dashboard_id}' not found.")
        if dashboard.recipe:
            raise DashboardValidationError("Dashboard is already live; use edit_dashboard to update sources.")
        if dashboard.status != STATUS_ACTIVE:
            raise DashboardNotRefreshableError("Dashboard is not active; renew it before converting to live.")
        if cadence not in CADENCES:
            raise DashboardValidationError(
                f"Invalid cadence '{cadence}': must be one of {', '.join(CADENCES)}."
            )
        valid_time = validate_refresh_time(refresh_time)
        content = self._contents.get(dashboard.id)
        if content is None or not content.html:
            raise DashboardNotRefreshableError("No HTML template stored for this dashboard.")

        try:
            norm_secrets = normalize_secrets(secrets)
            norm_recipe = normalize_recipe(sources)
            validate_secret_refs(norm_recipe, norm_secrets)
        except SourceValidationError as exc:
            raise DashboardValidationError(str(exc)) from exc

        try:
            dataset = self._collect_dataset(norm_recipe, norm_secrets, current_now)
        except SourceCollectionError as exc:
            raise DashboardValidationError(f"Data source failed during conversion: {exc}") from exc

        dashboard.recipe = norm_recipe
        self._store_secrets(dashboard, norm_secrets)
        dashboard.cadence = cadence
        dashboard.refresh_time = valid_time
        dashboard.next_refresh_at = next_refresh_at(cadence, current_now, valid_time)
        dashboard.updated_at = current_now.isoformat()
        dashboard.version += 1
        dashboard.consecutive_failures = 0
        dashboard.last_error = ""
        dashboard.size_bytes = _dataset_size_bytes(dataset) + len(content.html.encode("utf-8"))
        content.dataset = dataset

        self._record_history(
            dashboard,
            author,
            "convert_to_live",
            [f"sources: {summarize_sources(norm_recipe)}", f"cadence: {cadence}"],
            now=current_now,
        )
        return {**dashboard.to_public_dict(now=current_now), "rows": dataset["rows"]}

    def get(self, dashboard_id: str) -> PublishedDashboard | None:
        """Retrieve a dashboard by its ID."""
        return self._dashboards.get(dashboard_id)

    def get_by_access_token(self, access_token: str) -> PublishedDashboard | None:
        """Look up a dashboard by its read-only access token."""
        if not access_token:
            return None
        for dash in self._dashboards.values():
            if hmac.compare_digest(dash.access_token, access_token):
                return dash
        return None

    def get_by_renewal_token(self, renewal_token: str) -> PublishedDashboard | None:
        """Look up a dashboard by its renewal token."""
        if not renewal_token:
            return None
        for dash in self._dashboards.values():
            if hmac.compare_digest(dash.renewal_token, renewal_token):
                return dash
        return None

    def get_by_edit_token(self, edit_token: str) -> PublishedDashboard | None:
        """Look up and authenticate a dashboard by its one-time edit token ('ed_...')."""
        cleaned = (edit_token or "").strip()
        if not cleaned.startswith("ed_"):
            return None
        for dash in self._dashboards.values():
            if verify_edit_token(cleaned, dash.edit_token_hash):
                return dash
        return None

    def rotate_edit_token(
        self,
        dashboard_id: str,
        *,
        author: str = "owner",
        now: dt.datetime | None = None,
    ) -> str:
        """Issue a new edit token for a dashboard, invalidating the previous edit token."""
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        dashboard = self.get(dashboard_id)
        if dashboard is None:
            raise DashboardValidationError(f"Dashboard '{dashboard_id}' not found.")
        new_token = self._issue_edit_token(dashboard, current_now)
        self._record_history(dashboard, author, "rotate_edit_token", ["edit token rotated"], now=current_now)
        return new_token

    def publication_meta(self, dashboard: PublishedDashboard) -> dict[str, Any]:
        """Build clock-independent metadata injected as window.NEXDASH_META."""
        return {
            "id": dashboard.id,
            "title": dashboard.title,
            "refreshable": bool(dashboard.recipe),
            "updated_at": dashboard.updated_at,
            "version": dashboard.version,
            "cadence": dashboard.cadence,
            "refresh_time": dashboard.refresh_time,
            "next_refresh_at": dashboard.next_refresh_at,
            "last_error": dashboard.last_error if dashboard.consecutive_failures else "",
            "cooldown_minutes": REFRESH_COOLDOWN_MINUTES,
            "permanent": dashboard.permanent,
            "notes": [n.get("text", "") for n in dashboard.notes],
        }

    def render_by_access_token(
        self,
        access_token: str,
        *,
        now: dt.datetime | None = None,
    ) -> RenderedDashboard | None:
        """Render a dashboard by its access token with injected data and CSP headers.

        Returns None if the token does not exist, the dashboard is not active, or the
        dashboard has expired.
        """
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        dashboard = self.get_by_access_token(access_token)
        if dashboard is None or dashboard.status != STATUS_ACTIVE:
            return None
        if not dashboard.permanent and dashboard.expires_at and parse_iso_datetime(dashboard.expires_at) <= current_now:
            return None
        content = self._contents.get(dashboard.id)
        if content is None or not content.html:
            return None

        dashboard.access_count += 1
        dashboard.last_accessed_at = current_now.isoformat()
        dashboard.idle_warned_at = ""

        meta = self.publication_meta(dashboard)
        if dashboard.recipe and content.dataset is not None:
            rendered_html = inject_dashboard_data(content.html, content.dataset, meta)
        else:
            rendered_html = inject_dashboard_data(content.html, {}, meta, inject_footer=False)

        return RenderedDashboard(
            dashboard=dashboard,
            html=rendered_html,
            headers=build_csp_headers(),
        )

    def refresh_dashboard(
        self,
        dashboard_or_id: PublishedDashboard | str,
        *,
        reason: str = "manual",
        enforce_cooldown: bool = False,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Re-collect data for a live dashboard.

        Fail-safe: if any source fails, the previous dataset remains intact so the
        dashboard continues serving its last known good data.
        """
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        dashboard = (
            self.get(dashboard_or_id) if isinstance(dashboard_or_id, str) else dashboard_or_id
        )
        if dashboard is None:
            raise DashboardValidationError("Dashboard not found.")
        if not dashboard.recipe:
            raise DashboardNotRefreshableError(
                "Static snapshot dashboards cannot be refreshed; convert to live first."
            )
        if dashboard.status != STATUS_ACTIVE:
            raise DashboardNotRefreshableError("Dashboard is not active; renew it before refreshing.")
        if not dashboard.permanent and dashboard.expires_at and parse_iso_datetime(dashboard.expires_at) <= current_now:
            raise DashboardNotRefreshableError("Dashboard has expired; renew it before refreshing.")

        if enforce_cooldown:
            allowed, wait_seconds = can_refresh_now(dashboard, current_now)
            if not allowed:
                return {
                    "ok": False,
                    "rate_limited": True,
                    "retry_after_seconds": wait_seconds,
                    "version": dashboard.version,
                }

        secrets_map: dict[str, str] = {}
        try:
            secrets_map = self._load_secrets(dashboard)
            dataset = self._collect_dataset(dashboard.recipe, secrets_map, current_now)
        except Exception as exc:
            masked_err = mask_secrets(str(exc), secrets_map)[:180]
            dashboard.consecutive_failures += 1
            dashboard.last_error = masked_err
            dashboard.next_refresh_at = next_refresh_at(
                dashboard.cadence, current_now, dashboard.refresh_time
            )
            self._record_history(
                dashboard,
                reason,
                "refresh_failed",
                [f"failed: {masked_err[:120]}"],
                now=current_now,
            )
            return {
                "ok": False,
                "error": dashboard.last_error,
                "consecutive_failures": dashboard.consecutive_failures,
                "version": dashboard.version,
            }

        content = self._contents[dashboard.id]
        content.dataset = dataset
        dashboard.version += 1
        dashboard.updated_at = current_now.isoformat()
        dashboard.consecutive_failures = 0
        dashboard.last_error = ""
        dashboard.size_bytes = _dataset_size_bytes(dataset) + len(content.html.encode("utf-8"))
        dashboard.next_refresh_at = next_refresh_at(
            dashboard.cadence, current_now, dashboard.refresh_time
        )
        if reason != "scheduled":
            self._record_history(
                dashboard,
                reason,
                "refresh",
                [f"version {dashboard.version}"],
                now=current_now,
            )
        return {
            "ok": True,
            "version": dashboard.version,
            "updated_at": dashboard.updated_at,
            "rows": dataset["rows"],
        }

    def renew_dashboard(
        self,
        renewal_token_or_id: str,
        *,
        author: str = "owner",
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Extend a dashboard's expiry by +30 days (capped at +60 days from `now`).

        Accepts either the dashboard's `renewal_token` or its `id`.
        """
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        dashboard = self.get_by_renewal_token(renewal_token_or_id) or self.get(renewal_token_or_id)
        if dashboard is None:
            raise DashboardAuthError("Invalid renewal token or dashboard ID.")
        if dashboard.status == STATUS_REMOVED:
            raise DashboardNotRenewableError(
                f"Dashboard expired more than {GRACE_PERIOD_DAYS} days ago and its content was removed."
            )

        if not dashboard.permanent:
            dashboard.expires_at = compute_new_expiry(dashboard.expires_at, current_now).isoformat()
        dashboard.status = STATUS_ACTIVE
        dashboard.warnings_sent = []
        dashboard.renewal_count += 1
        self._record_history(
            dashboard,
            author,
            "renew",
            [f"expires_at -> {dashboard.expires_at[:10]}"],
            renewed=True,
            now=current_now,
        )
        return dashboard.to_public_dict(now=current_now)

    def edit_dashboard(
        self,
        edit_token: str,
        *,
        renew: bool,
        author: str = "agent",
        title: str | None = None,
        html: str | None = None,
        sources: list[dict[str, Any]] | dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
        cadence: str | None = None,
        refresh_time: str | None = None,
        note: str | None = None,
        clear_notes: bool = False,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Apply an edit authenticated by the dashboard's `edit_token`.

        - If `sources` or `secrets` change, data is re-collected immediately (bypassing
          the 15-minute manual click cooldown).
        - `secrets` are merged with stored secrets (an empty string removes a secret key).
        - `renew` is an explicit boolean so the caller chooses whether to extend validity by +30 days.
        """
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        dashboard = self.get_by_edit_token(edit_token)
        if dashboard is None:
            raise DashboardAuthError("Invalid edit token.")
        if dashboard.status == STATUS_REMOVED:
            raise DashboardNotRenewableError("Dashboard content was already removed; publish a new one.")

        changes: list[str] = []
        recipe_changed = False

        if title is not None and title.strip() and title.strip() != dashboard.title:
            dashboard.title = title.strip()[:200]
            changes.append(f"title -> {dashboard.title}")

        if clear_notes and dashboard.notes:
            dashboard.notes = []
            changes.append("notes cleared")

        if note is not None and note.strip():
            if len(dashboard.notes) >= MAX_NOTES:
                raise DashboardValidationError(f"Maximum of {MAX_NOTES} notes reached; clear notes first.")
            dashboard.notes.append(
                {
                    "text": note.strip()[:MAX_NOTE_CHARS],
                    "author": author,
                    "at": current_now.isoformat(),
                }
            )
            changes.append("note added")

        if html is not None and html.strip():
            if len(html.encode("utf-8")) > MAX_HTML_BYTES:
                raise DashboardValidationError(
                    f"HTML exceeds maximum size of {MAX_HTML_BYTES // (1024 * 1024)} MB."
                )
            self._contents[dashboard.id].html = html
            changes.append("HTML template updated")

        if sources is not None or secrets is not None:
            if not dashboard.recipe and sources is None:
                raise DashboardNotRefreshableError(
                    "Snapshot dashboard has no sources; use convert_to_live or provide sources."
                )
            try:
                current_secrets = self._load_secrets(dashboard)
            except SourceValidationError:
                current_secrets = {}
            try:
                merged_secrets = merge_secrets(current_secrets, secrets)
                target_recipe_input = sources if sources is not None else dashboard.recipe
                new_recipe = normalize_recipe(target_recipe_input)
                validate_secret_refs(new_recipe, merged_secrets)
            except SourceValidationError as exc:
                raise DashboardValidationError(str(exc)) from exc

            if new_recipe != dashboard.recipe:
                dashboard.recipe = new_recipe
                recipe_changed = True
                changes.append(f"sources -> {summarize_sources(new_recipe)}")
            if merged_secrets != current_secrets:
                changed_keys = sorted(
                    (set(merged_secrets) ^ set(current_secrets))
                    | {k for k in merged_secrets if current_secrets.get(k) != merged_secrets[k]}
                )
                self._store_secrets(dashboard, merged_secrets)
                recipe_changed = True
                changes.append(f"secrets updated: {', '.join(changed_keys)}")

        if refresh_time is not None and refresh_time.strip():
            valid_time = validate_refresh_time(refresh_time)
            if valid_time != dashboard.refresh_time:
                dashboard.refresh_time = valid_time
                dashboard.next_refresh_at = next_refresh_at(
                    dashboard.cadence, current_now, dashboard.refresh_time
                )
                changes.append(f"refresh_time -> {valid_time}")

        if cadence is not None and cadence != dashboard.cadence:
            if cadence not in CADENCES:
                raise DashboardValidationError(
                    f"Invalid cadence '{cadence}': must be one of {', '.join(CADENCES)}."
                )
            if cadence != "never" and not dashboard.recipe:
                raise DashboardNotRefreshableError("Snapshot dashboard cannot have a refresh cadence.")
            dashboard.cadence = cadence
            dashboard.next_refresh_at = next_refresh_at(
                dashboard.cadence, current_now, dashboard.refresh_time
            )
            changes.append(f"cadence -> {cadence}")

        renewed = False
        if renew and not dashboard.permanent:
            dashboard.expires_at = compute_new_expiry(dashboard.expires_at, current_now).isoformat()
            dashboard.status = STATUS_ACTIVE
            dashboard.warnings_sent = []
            dashboard.renewal_count += 1
            renewed = True

        dashboard.edit_count += 1
        self._record_history(dashboard, author, "edit", changes, renewed=renewed, now=current_now)

        refresh_result: dict[str, Any] | None = None
        if recipe_changed:
            if dashboard.status != STATUS_ACTIVE:
                refresh_result = {"ok": False, "error": "Dashboard is inactive; renew to refresh."}
            else:
                refresh_result = self.refresh_dashboard(
                    dashboard,
                    reason=f"edit:{author}",
                    enforce_cooldown=False,
                    now=current_now,
                )

        return {
            "changes": changes or ["no content changes"],
            "renewed": renewed,
            "refresh": refresh_result,
            "dashboard": dashboard.to_public_dict(now=current_now),
        }

    def process_expirations(self, *, now: dt.datetime | None = None) -> dict[str, int]:
        """Run one tick of the expiration and grace-period cleanup job."""
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        summary = {
            "evaluated": 0,
            "warnings": 0,
            "expired": 0,
            "removed": 0,
            "idle_warnings": 0,
        }
        for dashboard in list(self._dashboards.values()):
            if dashboard.status not in (STATUS_ACTIVE, STATUS_EXPIRED):
                continue
            summary["evaluated"] += 1
            actions = expiry_actions(dashboard, current_now)
            if not actions:
                continue
            for warn_action in ("warn_d3", "warn_d0"):
                if warn_action in actions:
                    tag = warn_action.split("_")[1]
                    if tag not in dashboard.warnings_sent:
                        dashboard.warnings_sent.append(tag)
                    summary["warnings"] += 1
            if "warn_idle" in actions:
                dashboard.idle_warned_at = current_now.isoformat()
                summary["idle_warnings"] += 1
            if "expire" in actions:
                dashboard.status = STATUS_EXPIRED
                summary["expired"] += 1
            if "remove" in actions:
                dashboard.status = STATUS_REMOVED
                self._contents.pop(dashboard.id, None)
                summary["removed"] += 1
        return summary

    def process_scheduled_refreshes(self, *, now: dt.datetime | None = None) -> dict[str, int]:
        """Run one tick of the scheduled refresh job for all due live dashboards."""
        current_now = (now or _utc_now()).astimezone(dt.UTC)
        active = [d for d in self._dashboards.values() if d.status == STATUS_ACTIVE]
        due = due_for_refresh(active, current_now)
        summary = {
            "evaluated": len(active),
            "due": len(due),
            "refreshed": 0,
            "failed": 0,
        }
        for dashboard in due:
            res = self.refresh_dashboard(
                dashboard,
                reason="scheduled",
                enforce_cooldown=False,
                now=current_now,
            )
            if res.get("ok"):
                summary["refreshed"] += 1
            else:
                summary["failed"] += 1
        return summary

    def preview_sources(
        self,
        sources: list[dict[str, Any]] | dict[str, Any],
        secrets: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Preview data sources without publishing or modifying a dashboard."""
        return preview_recipe_sources(
            sources,
            secrets,
            allow_private=self.allow_private_ssrf,
            resolver=self.resolver,
            http_fetcher=self.http_fetcher,
            transport=self.transport,
            sql_executor=self.sql_executor,
            cube_registry=self.cube_registry,
        )
