"""Pluggable database access, engine factory, and schema initialization.

Supports SQLite by default (zero configuration) and external databases
(PostgreSQL, MySQL, Microsoft SQL Server, ODBC) via SQLAlchemy engines.
Configuration is resolved from environment variables (DB_URL) or .env files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

DEFAULT_SQLITE_URL = "sqlite:///:memory:"

metadata = sa.MetaData()

approval_codes_table = sa.Table(
    "nexdash_approval_codes",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("code_hash", sa.String(128), nullable=False, unique=True),
    sa.Column("created_by", sa.String(255), nullable=False),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("expires_at", sa.String(64), nullable=False),
    sa.Column("used_at", sa.String(64), nullable=True),
    sa.Column("used_for", sa.String(255), nullable=True),
)

reports_table = sa.Table(
    "nexdash_reports",
    metadata,
    sa.Column("key", sa.String(128), primary_key=True),
    sa.Column("published_version", sa.Integer, nullable=True),
    sa.Column("published_spec_json", sa.Text, nullable=True),
    sa.Column("draft_spec_json", sa.Text, nullable=True),
    sa.Column("draft_updated_at", sa.String(64), nullable=True),
    sa.Column("draft_author", sa.String(255), nullable=True),
    sa.Column("draft_note", sa.Text, nullable=False, server_default=""),
)

report_versions_table = sa.Table(
    "nexdash_report_versions",
    metadata,
    sa.Column("key", sa.String(128), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("spec_json", sa.Text, nullable=False),
    sa.Column("published_at", sa.String(64), nullable=False),
    sa.Column("published_by", sa.String(255), nullable=False),
    sa.Column("approval_code_id", sa.String(64), nullable=False),
    sa.Column("note", sa.Text, nullable=False, server_default=""),
    sa.Column("diff_summary", sa.Text, nullable=False, server_default=""),
    sa.Column("rollback_from", sa.Integer, nullable=True),
)


def load_env_file(dotenv_path: str | Path | None = None) -> dict[str, str]:
    """Load key-value pairs from a .env file into os.environ if not already present.

    Does not overwrite existing environment variables. Secrets remain in .env and
    are never committed to version control.
    """
    candidates: list[Path] = []
    if dotenv_path is not None:
        candidates.append(Path(dotenv_path))
    else:
        candidates.extend([Path(".env"), Path("../.env")])

    loaded: dict[str, str] = {}
    for candidate in candidates:
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8")
                for line in content.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "=" not in stripped:
                        continue
                    key, val = stripped.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    if key and key not in os.environ:
                        os.environ[key] = val
                        loaded[key] = val
                break
            except OSError:
                continue

    return loaded


def get_db_url(env_var: str = "DB_URL", default: str = DEFAULT_SQLITE_URL) -> str:
    """Return the database URL configured in the environment or fallback default.

    Checks DB_URL, then NEXDASH_DB_URL, loading .env if necessary.
    """
    load_env_file()
    url = os.environ.get(env_var) or os.environ.get("NEXDASH_DB_URL")
    if url and url.strip():
        return url.strip()
    return default


def sanitize_db_url(url: str) -> str:
    """Return a sanitized representation of a database URL with passwords masked."""
    if not url or "://" not in url:
        return url
    try:
        parts = urlsplit(url)
        if parts.password:
            masked_netloc = parts.netloc.replace(f":{parts.password}@", ":****@")
            return url.replace(parts.netloc, masked_netloc)
    except (ValueError, TypeError):
        return url
    return url


def create_database_engine(
    url_or_engine: str | sa.Engine | None = None,
    **kwargs: Any,
) -> sa.Engine:
    """Create a configured SQLAlchemy engine for any supported SQL database.

    Supports:
    - SQLite: sqlite:///... or :memory: (with thread-safety and in-memory pool handling)
    - PostgreSQL: postgresql+psycopg://...
    - MySQL: mysql+pymysql://...
    - SQL Server: mssql+pyodbc://...
    - ODBC connection strings
    """
    if isinstance(url_or_engine, sa.Engine):
        return url_or_engine

    target_url = url_or_engine if url_or_engine is not None else get_db_url()

    # Normalize plain file paths or :memory: into SQLite URLs
    if target_url == ":memory:":
        target_url = "sqlite:///:memory:"
    elif "://" not in target_url:
        resolved = Path(target_url).resolve()
        target_url = f"sqlite:///{resolved}"

    engine_kwargs: dict[str, Any] = dict(kwargs)

    # SQLite specific tuning
    if target_url.startswith("sqlite"):
        connect_args = dict(engine_kwargs.pop("connect_args", {}))
        connect_args.setdefault("check_same_thread", False)
        engine_kwargs["connect_args"] = connect_args
        if ":memory:" in target_url:
            engine_kwargs.setdefault("poolclass", StaticPool)
    else:
        # Remote engines: enable connection pre-ping to recover from idle drops
        engine_kwargs.setdefault("pool_pre_ping", True)

    return sa.create_engine(target_url, **engine_kwargs)


def init_db_schema(engine: sa.Engine) -> None:
    """Create all core NexDash tables on the target engine if they do not exist."""
    metadata.create_all(engine)


def check_database_connection(engine: sa.Engine) -> bool:
    """Verify that the engine can establish a connection and execute a basic query."""
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        return False
