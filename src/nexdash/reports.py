"""Managed reports lifecycle: draft, preview, diff, publish, and rollback with approval codes.

All report queries run strictly through the semantic layer (`Cube`, `QueryPlan`, `SQLCompiler`,
and `SQLiteExecutor`). The language model never writes free-form SQL; filter and parameter values
are always bound as query parameters.

Every state-changing (write) operation (`create_draft`, `edit_draft`, `discard_draft`, `publish`,
and `rollback`) requires a short-lived, single-use human approval code (`ApprovalCode`).
Validation errors do not consume the approval code so the caller can fix the specification and
retry with the same code.
"""

from __future__ import annotations

import copy
import datetime as dt
import difflib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from nexdash.compiler import AllowlistError, CompilationError, SQLCompiler
from nexdash.cube import (
    CubeRegistry,
    FilterOperator,
    validate_identifier,
    validate_qualified_identifier,
)
from nexdash.executor import SQLiteExecutor
from nexdash.plan import QueryPlan, TimeRange

# Unambiguous human-readable alphabet (omits 0, O, 1, I, L)
APPROVAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
APPROVAL_CODE_LENGTH = 8
DEFAULT_APPROVAL_TTL_MINUTES = 10
DEFAULT_PREVIEW_SAMPLE_LIMIT = 50

ChartType = Literal["bar", "line", "pie", "table", "kpi", "area"]
EditOpType = Literal["set", "add", "update", "remove", "move", "unset"]
DiffChangeType = Literal["added", "removed", "modified"]
PreviewSourceState = Literal["draft", "published", "ephemeral"]


class ReportError(Exception):
    """Base exception for report lifecycle and governance operations."""


class ReportValidationError(ReportError, ValueError):
    """Raised when a report specification, edit operation, or state transition is invalid."""


class ReportNotFoundError(ReportError, KeyError):
    """Raised when a report key or version does not exist."""


class ApprovalCodeError(ReportError, PermissionError):
    """Raised when a write operation is attempted without a valid, unused approval code."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_iso(iso_str: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(iso_str)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def normalize_approval_code(code: str) -> str:
    """Normalize an approval code by upper-casing and stripping non-alphanumeric characters."""
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def hash_approval_code(code: str) -> str:
    """Return the SHA-256 hex digest of a normalized approval code."""
    normalized = normalize_approval_code(code)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalCode:
    """A newly issued single-use approval code for authorizing a report write operation."""

    id: str
    code: str
    created_by: str
    created_at: str
    expires_at: str
    ttl_minutes: int


@dataclass(frozen=True)
class ApprovalCodeStatus:
    """Verification status of an approval code."""

    valid: bool
    id: str | None = None
    created_by: str | None = None
    reason: str | None = None


class ReportParameter(BaseModel):
    """A runtime parameter that maps user/caller inputs onto parameterized plan filters."""

    name: str
    field: str
    operator: FilterOperator = "eq"
    default: Any = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_identifier(v, "parameter name")

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        return validate_qualified_identifier(v, "parameter field")


class ReportColumn(BaseModel):
    """Display configuration for a column returned by a report's query plan."""

    field: str
    label: str | None = None
    format: str | None = None

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        return validate_qualified_identifier(v, "column field")


class ReportChart(BaseModel):
    """Declarative chart configuration computed from report query rows without LLM calls."""

    id: str
    type: ChartType = "bar"
    title: str | None = None
    x: str | None = None
    y: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        return validate_identifier(v, "chart id")

    @field_validator("x", "y")
    @classmethod
    def _validate_axis(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_qualified_identifier(v, "chart axis field")
        return v

    @model_validator(mode="after")
    def _validate_axes_for_type(self) -> ReportChart:
        if self.type in ("bar", "line", "pie", "area"):
            if not self.x or not self.y:
                raise ValueError(f"Chart '{self.id}' of type '{self.type}' requires both 'x' and 'y' fields")
        elif self.type == "kpi" and not self.y:
            raise ValueError(f"Chart '{self.id}' of type 'kpi' requires a 'y' metric field")
        return self


class ReportSpec(BaseModel):
    """Complete declarative specification of a report backed by a semantic QueryPlan."""

    title: str
    description: str | None = None
    plan: QueryPlan
    parameters: list[ReportParameter] = Field(default_factory=list)
    columns: list[ReportColumn] = Field(default_factory=list)
    charts: list[ReportChart] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        cleaned = str(v or "").strip()
        if not cleaned:
            raise ValueError("Report title must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _validate_unique_collections(self) -> ReportSpec:
        param_names: set[str] = set()
        for param in self.parameters:
            if param.name in param_names:
                raise ValueError(f"Duplicate parameter name '{param.name}' in ReportSpec")
            param_names.add(param.name)

        chart_ids: set[str] = set()
        for chart in self.charts:
            if chart.id in chart_ids:
                raise ValueError(f"Duplicate chart id '{chart.id}' in ReportSpec")
            chart_ids.add(chart.id)

        col_fields: set[str] = set()
        for col in self.columns:
            if col.field in col_fields:
                raise ValueError(f"Duplicate column field '{col.field}' in ReportSpec")
            col_fields.add(col.field)

        return self


class EditOperation(BaseModel):
    """A single structured patch operation applied to a ReportSpec draft."""

    op: EditOpType
    path: str
    value: Any = None
    id: str | None = None
    before: str | None = None
    position: int | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        cleaned = str(v or "").strip()
        if not cleaned:
            raise ValueError("EditOperation path must not be empty")
        return cleaned


@dataclass(frozen=True)
class DiffEntry:
    """A single field-level difference between two report specifications."""

    path: str
    change_type: DiffChangeType
    before: Any = None
    after: Any = None


@dataclass(frozen=True)
class ReportDiff:
    """Structured and unified diff between two report specifications."""

    has_changes: bool
    is_initial: bool
    changes: list[DiffEntry]
    unified_diff: str
    summary: str


@dataclass(frozen=True)
class ComputedChart:
    """Pre-computed chart series produced deterministically from query results."""

    id: str
    type: ChartType
    title: str
    x: str | None
    y: str | None
    labels: list[Any] = field(default_factory=list)
    values: list[Any] = field(default_factory=list)
    kpi_value: Any = None


@dataclass(frozen=True)
class ReportPreview:
    """Result of executing a report preview without mutating persisted state."""

    key: str
    source_state: PreviewSourceState
    spec: ReportSpec
    compiled_sql: str
    sql_params: dict[str, Any]
    columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    charts: list[ComputedChart]
    warnings: list[str]
    narrative: str | None = None


@dataclass(frozen=True)
class ReportVersion:
    """Immutable snapshot of a published report version."""

    version: int
    spec: ReportSpec
    published_at: str
    published_by: str
    approval_code_id: str
    note: str = ""
    diff_summary: str = ""
    rollback_from: int | None = None


@dataclass(frozen=True)
class ReportRecord:
    """Current state of a managed report including published version, draft, diff, and history."""

    key: str
    published_version: int | None
    published_spec: ReportSpec | None
    draft_spec: ReportSpec | None
    draft_updated_at: str | None
    draft_author: str | None
    draft_note: str
    diff: ReportDiff
    versions: list[ReportVersion] = field(default_factory=list)

    @property
    def has_draft(self) -> bool:
        return self.draft_spec is not None

    @property
    def is_published(self) -> bool:
        return self.published_spec is not None and self.published_version is not None


# ---------------------------------------------------------------------------
# Diff computation and structured edit patch application
# ---------------------------------------------------------------------------


def _flatten_dict(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts and lists into dot/bracket paths for structural comparison."""
    items: dict[str, Any] = {}
    if isinstance(data, dict):
        if not data and prefix:
            items[prefix] = {}
        for k, v in data.items():
            new_key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                items.update(_flatten_dict(v, new_key))
            elif isinstance(v, list):
                items[new_key] = v
            else:
                items[new_key] = v
    else:
        items[prefix] = data
    return items


def compute_spec_diff(
    before: ReportSpec | dict[str, Any] | None,
    after: ReportSpec | dict[str, Any] | None,
) -> ReportDiff:
    """Compute a structured and unified diff between two ReportSpec objects.

    Args:
        before: Baseline specification (typically the published spec, or None if unpublished).
        after: Target specification (typically the pending draft spec).

    Returns:
        A ReportDiff describing all added, removed, and modified paths plus a unified diff.
    """
    before_dict: dict[str, Any] | None = (
        before.model_dump(mode="json") if isinstance(before, ReportSpec) else copy.deepcopy(before)
    )
    after_dict: dict[str, Any] | None = (
        after.model_dump(mode="json") if isinstance(after, ReportSpec) else copy.deepcopy(after)
    )

    if before_dict is None and after_dict is None:
        return ReportDiff(
            has_changes=False,
            is_initial=False,
            changes=[],
            unified_diff="",
            summary="No changes",
        )

    is_initial = before_dict is None and after_dict is not None

    before_flat = _flatten_dict(before_dict) if before_dict is not None else {}
    after_flat = _flatten_dict(after_dict) if after_dict is not None else {}

    all_paths = sorted(set(before_flat.keys()) | set(after_flat.keys()))
    changes: list[DiffEntry] = []

    for path in all_paths:
        in_before = path in before_flat
        in_after = path in after_flat
        if not in_before and in_after:
            changes.append(DiffEntry(path=path, change_type="added", before=None, after=after_flat[path]))
        elif in_before and not in_after:
            changes.append(DiffEntry(path=path, change_type="removed", before=before_flat[path], after=None))
        elif before_flat[path] != after_flat[path]:
            changes.append(
                DiffEntry(
                    path=path,
                    change_type="modified",
                    before=before_flat[path],
                    after=after_flat[path],
                )
            )

    before_lines = (
        json.dumps(before_dict, indent=2, sort_keys=True, ensure_ascii=True).splitlines(keepends=True)
        if before_dict is not None
        else []
    )
    after_lines = (
        json.dumps(after_dict, indent=2, sort_keys=True, ensure_ascii=True).splitlines(keepends=True)
        if after_dict is not None
        else []
    )

    unified = "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="published",
            tofile="draft",
        )
    )

    has_changes = len(changes) > 0
    if not has_changes:
        summary = "No changes"
    elif is_initial:
        summary = f"Initial draft ({len(changes)} fields configured)"
    else:
        added = [c.path for c in changes if c.change_type == "added"]
        modified = [c.path for c in changes if c.change_type == "modified"]
        removed = [c.path for c in changes if c.change_type == "removed"]
        parts: list[str] = []
        if modified:
            parts.append("Modified: " + ", ".join(modified))
        if added:
            parts.append("Added: " + ", ".join(added))
        if removed:
            parts.append("Removed: " + ", ".join(removed))
        summary = "; ".join(parts)

    return ReportDiff(
        has_changes=has_changes,
        is_initial=is_initial,
        changes=changes,
        unified_diff=unified,
        summary=summary,
    )


def _match_list_item_key(item: Any, target_id: str) -> bool:
    """Match an element in a list of dicts by 'id', 'name', or 'field'."""
    if not isinstance(item, dict):
        return str(item) == target_id
    for key in ("id", "name", "field"):
        if key in item and str(item[key]) == target_id:
            return True
    return False


def _resolve_parent_and_key(root: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    """Navigate a dotted path in a dictionary and return (parent_dict, final_key)."""
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise ReportValidationError("Empty operation path")
    curr: Any = root
    for part in parts[:-1]:
        if not isinstance(curr, dict):
            raise ReportValidationError(f"Path segment '{part}' in '{path}' does not traverse a mapping")
        if part not in curr or curr[part] is None:
            curr[part] = {}
        curr = curr[part]
    if not isinstance(curr, dict):
        raise ReportValidationError(f"Target parent for path '{path}' is not a mapping")
    return curr, parts[-1]


def apply_edit_operations(
    base_spec: ReportSpec,
    operations: list[EditOperation | dict[str, Any]],
) -> ReportSpec:
    """Apply a sequence of structured EditOperations to a ReportSpec and validate the result.

    Supported operations:
    - `set`: set the value at `path` (e.g. `title`, `plan.limit`, `plan.metrics`)
    - `unset`: reset `path` to None or empty list
    - `add`: append `value` to list at `path` (or insert before item matching `before`)
    - `update`: update item matching `id` inside list at `path` with fields in `value`
    - `remove`: remove item matching `id` (or value) from list at `path`
    - `move`: move item matching `id` inside list at `path` to index `position`
    """
    raw = base_spec.model_dump(mode="python")

    for raw_op in operations:
        try:
            op_obj = raw_op if isinstance(raw_op, EditOperation) else EditOperation.model_validate(raw_op)
        except Exception as exc:
            raise ReportValidationError(f"Invalid edit operation: {exc}") from exc

        parent, key = _resolve_parent_and_key(raw, op_obj.path)
        op = op_obj.op

        if op == "set":
            parent[key] = copy.deepcopy(op_obj.value)

        elif op == "unset":
            if key in parent:
                if isinstance(parent[key], list):
                    parent[key] = []
                else:
                    parent[key] = None

        elif op == "add":
            target_list = parent.get(key)
            if not isinstance(target_list, list):
                raise ReportValidationError(f"Operation 'add' requires path '{op_obj.path}' to be a list")
            new_item = copy.deepcopy(op_obj.value)
            if op_obj.before:
                inserted = False
                for idx, existing in enumerate(target_list):
                    if _match_list_item_key(existing, op_obj.before):
                        target_list.insert(idx, new_item)
                        inserted = True
                        break
                if not inserted:
                    target_list.append(new_item)
            elif op_obj.position is not None:
                pos = max(0, min(len(target_list), op_obj.position))
                target_list.insert(pos, new_item)
            else:
                target_list.append(new_item)

        elif op == "update":
            target_list = parent.get(key)
            if not isinstance(target_list, list):
                raise ReportValidationError(f"Operation 'update' requires path '{op_obj.path}' to be a list")
            if not op_obj.id:
                raise ReportValidationError("Operation 'update' requires an 'id' selector")
            found = False
            for idx, existing in enumerate(target_list):
                if _match_list_item_key(existing, op_obj.id):
                    if isinstance(existing, dict) and isinstance(op_obj.value, dict):
                        existing.update(copy.deepcopy(op_obj.value))
                    else:
                        target_list[idx] = copy.deepcopy(op_obj.value)
                    found = True
                    break
            if not found:
                raise ReportValidationError(
                    f"Item '{op_obj.id}' not found in '{op_obj.path}' for 'update' operation"
                )

        elif op == "remove":
            target_list = parent.get(key)
            if not isinstance(target_list, list):
                raise ReportValidationError(f"Operation 'remove' requires path '{op_obj.path}' to be a list")
            selector = op_obj.id if op_obj.id is not None else str(op_obj.value)
            new_list = [item for item in target_list if not _match_list_item_key(item, selector)]
            if len(new_list) == len(target_list):
                raise ReportValidationError(
                    f"Item '{selector}' not found in '{op_obj.path}' for 'remove' operation"
                )
            parent[key] = new_list

        elif op == "move":
            target_list = parent.get(key)
            if not isinstance(target_list, list):
                raise ReportValidationError(f"Operation 'move' requires path '{op_obj.path}' to be a list")
            if not op_obj.id or op_obj.position is None:
                raise ReportValidationError("Operation 'move' requires both 'id' and 'position'")
            found_idx = -1
            for idx, existing in enumerate(target_list):
                if _match_list_item_key(existing, op_obj.id):
                    found_idx = idx
                    break
            if found_idx == -1:
                raise ReportValidationError(
                    f"Item '{op_obj.id}' not found in '{op_obj.path}' for 'move' operation"
                )
            item = target_list.pop(found_idx)
            pos = max(0, min(len(target_list), op_obj.position))
            target_list.insert(pos, item)

    try:
        return ReportSpec.model_validate(raw)
    except Exception as exc:
        raise ReportValidationError(f"Edited report specification is invalid: {exc}") from exc


# ---------------------------------------------------------------------------
# Deterministic chart computation and parameter binding
# ---------------------------------------------------------------------------


def bind_runtime_parameters(
    spec: ReportSpec,
    runtime_params: dict[str, Any] | None = None,
) -> QueryPlan:
    """Produce a QueryPlan with declared ReportParameters and runtime overrides bound as PlanFilters.

    All values are validated through PlanFilter / TimeRange and compiled as bound SQL parameters.
    Free-form SQL is never accepted.
    """
    params = dict(runtime_params or {})
    plan_dict = spec.plan.model_dump(mode="python")
    filters_list: list[dict[str, Any]] = list(plan_dict.get("filters") or [])

    declared_by_name = {p.name: p for p in spec.parameters}

    # Check for unknown runtime parameters (except reserved 'time_range' dict)
    for param_name in params:
        if param_name == "time_range":
            continue
        if param_name not in declared_by_name:
            raise ReportValidationError(
                f"Unknown runtime parameter '{param_name}'. Declared parameters: {sorted(declared_by_name.keys())}"
            )

    # Apply declared parameters (using runtime value if supplied, else default if non-None)
    for param_def in spec.parameters:
        val = params.get(param_def.name, param_def.default)
        if val is None:
            continue
        # Replace existing filter on same field+operator or append
        replaced = False
        for idx, existing_flt in enumerate(filters_list):
            if existing_flt.get("field") == param_def.field and existing_flt.get("operator") == param_def.operator:
                filters_list[idx] = {
                    "field": param_def.field,
                    "operator": param_def.operator,
                    "value": val,
                }
                replaced = True
                break
        if not replaced:
            filters_list.append(
                {
                    "field": param_def.field,
                    "operator": param_def.operator,
                    "value": val,
                }
            )

    plan_dict["filters"] = filters_list

    if "time_range" in params and params["time_range"] is not None:
        tr_override = params["time_range"]
        if isinstance(tr_override, TimeRange):
            plan_dict["time_range"] = tr_override.model_dump(mode="python")
        elif isinstance(tr_override, dict):
            plan_dict["time_range"] = dict(tr_override)
        else:
            raise ReportValidationError("Runtime 'time_range' override must be a mapping or TimeRange")

    try:
        return QueryPlan.model_validate(plan_dict)
    except Exception as exc:
        raise ReportValidationError(f"Failed to bind runtime parameters into QueryPlan: {exc}") from exc


def compute_charts_from_rows(
    spec: ReportSpec,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> tuple[list[ComputedChart], list[str]]:
    """Compute chart series deterministically from query rows without calling an LLM.

    Returns:
        Tuple of (computed_charts, warnings).
    """
    computed: list[ComputedChart] = []
    warnings: list[str] = []
    col_set = set(columns)

    for col_cfg in spec.columns:
        if col_cfg.field not in col_set:
            warnings.append(f"Configured column '{col_cfg.field}' is not present in query result columns {columns}")

    for chart in spec.charts:
        title = chart.title or chart.id
        if chart.type == "table":
            computed.append(
                ComputedChart(
                    id=chart.id,
                    type=chart.type,
                    title=title,
                    x=chart.x,
                    y=chart.y,
                    labels=list(columns),
                    values=list(rows),
                )
            )
            continue

        if chart.type == "kpi":
            if chart.y not in col_set:
                warnings.append(
                    f"Chart '{chart.id}' references metric '{chart.y}' which is not in query result columns {columns}"
                )
                kpi_val = None
            else:
                kpi_val = rows[0].get(chart.y) if rows else None
            computed.append(
                ComputedChart(
                    id=chart.id,
                    type=chart.type,
                    title=title,
                    x=None,
                    y=chart.y,
                    kpi_value=kpi_val,
                )
            )
            continue

        # bar, line, pie, area
        missing_axes: list[str] = []
        if chart.x and chart.x not in col_set:
            missing_axes.append(chart.x)
        if chart.y and chart.y not in col_set:
            missing_axes.append(chart.y)

        if missing_axes:
            warnings.append(
                f"Chart '{chart.id}' references field(s) {missing_axes} not in query result columns {columns}"
            )
            computed.append(
                ComputedChart(
                    id=chart.id,
                    type=chart.type,
                    title=title,
                    x=chart.x,
                    y=chart.y,
                )
            )
            continue

        labels = [row.get(chart.x or "") for row in rows]
        values = [row.get(chart.y or "") for row in rows]
        computed.append(
            ComputedChart(
                id=chart.id,
                type=chart.type,
                title=title,
                x=chart.x,
                y=chart.y,
                labels=labels,
                values=values,
            )
        )

    return computed, warnings


def generate_report_narrative(
    spec: ReportSpec,
    rows: list[dict[str, Any]],
    *,
    tier: str = "small",
    config: Any | None = None,
) -> str:
    """Generate a short analytical summary of report results using nexcoder-core's tiered model registry."""
    from nexcoder_core import NexcoderConfig, get_chat_model

    cfg = config if config is not None else NexcoderConfig(preset="offline")
    model = get_chat_model(tier, config=cfg)
    payload = {
        "title": spec.title,
        "cube": spec.plan.cube,
        "metrics": spec.plan.metrics,
        "dimensions": spec.plan.dimensions,
        "row_count": len(rows),
        "sample_rows": rows[:20],
    }
    prompt = (
        "Summarize the following analytical report results in 2 to 4 concise sentences. "
        "Rely strictly on the provided numbers.\n\n"
        f"DATA:\n{json.dumps(payload, default=str)}"
    )
    response = model.invoke(prompt)
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = " ".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
    return str(content).strip()


# ---------------------------------------------------------------------------
# ReportStore: SQLite-backed report lifecycle and approval code manager
# ---------------------------------------------------------------------------


class ReportStore:
    """Manages report drafts, previews, diffs, publications, rollbacks, and approval codes.

    Every write operation (`create_draft`, `edit_draft`, `discard_draft`, `publish`, `rollback`)
    requires a valid, unexpired, single-use approval code generated via `generate_approval_code`.
    Validation errors do not consume the approval code.
    """

    def __init__(
        self,
        database: sqlite3.Connection | str | Path = ":memory:",
        *,
        registry: CubeRegistry | None = None,
        executor: SQLiteExecutor | None = None,
        compiler: SQLCompiler | None = None,
    ) -> None:
        self.registry = registry
        self.executor = executor
        self.compiler = compiler or (executor.compiler if executor else SQLCompiler(default_dialect="sqlite"))
        self._owns_connection = False

        if isinstance(database, sqlite3.Connection):
            self.connection = database
        else:
            self.connection = sqlite3.connect(str(database))
            self._owns_connection = True

        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS nexdash_approval_codes (
                id TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL UNIQUE,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                used_for TEXT
            );

            CREATE TABLE IF NOT EXISTS nexdash_reports (
                key TEXT PRIMARY KEY,
                published_version INTEGER,
                published_spec_json TEXT,
                draft_spec_json TEXT,
                draft_updated_at TEXT,
                draft_author TEXT,
                draft_note TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS nexdash_report_versions (
                key TEXT NOT NULL,
                version INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                published_at TEXT NOT NULL,
                published_by TEXT NOT NULL,
                approval_code_id TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                diff_summary TEXT NOT NULL DEFAULT '',
                rollback_from INTEGER,
                PRIMARY KEY (key, version)
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        """Close the store's SQLite connection if owned by this instance."""
        if self._owns_connection and self.connection:
            self.connection.close()

    def __enter__(self) -> ReportStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # -----------------------------------------------------------------------
    # Approval Code Governance
    # -----------------------------------------------------------------------

    def generate_approval_code(
        self,
        created_by: str = "admin",
        *,
        ttl_minutes: int = DEFAULT_APPROVAL_TTL_MINUTES,
        now: dt.datetime | None = None,
    ) -> ApprovalCode:
        """Issue a single-use human approval code required for report write operations.

        Only the SHA-256 hash of the code is persisted in the database; the plaintext code
        is returned only in this response.
        """
        actor = str(created_by or "").strip()
        if not actor:
            raise ReportValidationError("Approval code creator ('created_by') must not be empty")
        if ttl_minutes <= 0:
            raise ReportValidationError("Approval code 'ttl_minutes' must be positive")

        current_time = now or _utc_now()
        expires_at = current_time + dt.timedelta(minutes=ttl_minutes)
        raw_chars = "".join(secrets.choice(APPROVAL_CODE_ALPHABET) for _ in range(APPROVAL_CODE_LENGTH))
        formatted_code = f"{raw_chars[:4]}-{raw_chars[4:]}"
        code_id = str(uuid.uuid4())
        code_hash = hash_approval_code(formatted_code)

        self.connection.execute(
            """
            INSERT INTO nexdash_approval_codes (
                id, code_hash, created_by, created_at, expires_at, used_at, used_for
            ) VALUES (:id, :code_hash, :created_by, :created_at, :expires_at, NULL, NULL)
            """,
            {
                "id": code_id,
                "code_hash": code_hash,
                "created_by": actor,
                "created_at": current_time.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
        )
        self.connection.commit()

        return ApprovalCode(
            id=code_id,
            code=formatted_code,
            created_by=actor,
            created_at=current_time.isoformat(),
            expires_at=expires_at.isoformat(),
            ttl_minutes=ttl_minutes,
        )

    def verify_approval_code(
        self,
        code: str | None,
        *,
        now: dt.datetime | None = None,
    ) -> ApprovalCodeStatus:
        """Check whether an approval code is valid without consuming it."""
        if not code or not str(code).strip():
            return ApprovalCodeStatus(valid=False, reason="Approval code is required for write operations.")

        normalized = normalize_approval_code(code)
        if len(normalized) != APPROVAL_CODE_LENGTH:
            return ApprovalCodeStatus(valid=False, reason="Invalid approval code format.")

        target_hash = hash_approval_code(normalized)
        row = self.connection.execute(
            "SELECT * FROM nexdash_approval_codes WHERE code_hash = :code_hash",
            {"code_hash": target_hash},
        ).fetchone()

        if row is None or not hmac.compare_digest(str(row["code_hash"]), target_hash):
            return ApprovalCodeStatus(valid=False, reason="Unknown approval code.")

        if row["used_at"] is not None:
            return ApprovalCodeStatus(
                valid=False,
                id=str(row["id"]),
                created_by=str(row["created_by"]),
                reason="Approval code has already been used. Generate a new approval code.",
            )

        current_time = now or _utc_now()
        if _parse_iso(str(row["expires_at"])) < current_time:
            return ApprovalCodeStatus(
                valid=False,
                id=str(row["id"]),
                created_by=str(row["created_by"]),
                reason="Approval code has expired. Generate a new approval code.",
            )

        return ApprovalCodeStatus(
            valid=True,
            id=str(row["id"]),
            created_by=str(row["created_by"]),
        )

    def _require_valid_code(
        self,
        code: str | None,
        *,
        now: dt.datetime | None = None,
    ) -> ApprovalCodeStatus:
        status = self.verify_approval_code(code, now=now)
        if not status.valid:
            raise ApprovalCodeError(status.reason or "Invalid approval code.")
        return status

    def _consume_code(
        self,
        code_id: str,
        action: str,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        current_time = now or _utc_now()
        cursor = self.connection.execute(
            """
            UPDATE nexdash_approval_codes
               SET used_at = :used_at,
                   used_for = :used_for
             WHERE id = :id AND used_at IS NULL
            """,
            {
                "id": code_id,
                "used_at": current_time.isoformat(),
                "used_for": str(action)[:200],
            },
        )
        if cursor.rowcount != 1:
            raise ApprovalCodeError("Approval code could not be consumed because it was already used.")

    # -----------------------------------------------------------------------
    # Validation Helpers
    # -----------------------------------------------------------------------

    def _coerce_and_validate_spec(self, spec: ReportSpec | dict[str, Any]) -> ReportSpec:
        try:
            validated = spec if isinstance(spec, ReportSpec) else ReportSpec.model_validate(spec)
        except Exception as exc:
            raise ReportValidationError(f"Invalid report specification: {exc}") from exc

        # Validate parameter fields and compilation against the semantic CubeRegistry if provided
        if self.registry is not None:
            if validated.plan.cube not in self.registry:
                raise ReportValidationError(
                    f"Cube '{validated.plan.cube}' referenced by report does not exist in registry"
                )
            cube = self.registry.get(validated.plan.cube)
            try:
                effective_plan = bind_runtime_parameters(validated)
                self.compiler.compile(effective_plan, cube, registry=self.registry)
            except (AllowlistError, CompilationError) as exc:
                raise ReportValidationError(f"Report query plan failed semantic guard validation: {exc}") from exc

            for param in validated.parameters:
                if (
                    "." not in param.field
                    and param.field not in cube.allowed_metric_names
                    and not cube.is_filter_allowed(param.field, param.operator)
                ):
                    raise ReportValidationError(
                        f"Report parameter '{param.name}' on field '{param.field}' "
                        f"with operator '{param.operator}' is not allowed in cube '{cube.name}'"
                    )

        return validated

    # -----------------------------------------------------------------------
    # Read / Query Operations
    # -----------------------------------------------------------------------

    def get_report(self, key: str) -> ReportRecord:
        """Retrieve a report's published state, pending draft, diff, and version history."""
        validate_identifier(key, "report key")
        row = self.connection.execute(
            "SELECT * FROM nexdash_reports WHERE key = :key",
            {"key": key},
        ).fetchone()
        if row is None:
            raise ReportNotFoundError(f"Report '{key}' not found")

        published_spec = (
            ReportSpec.model_validate(json.loads(row["published_spec_json"]))
            if row["published_spec_json"]
            else None
        )
        draft_spec = (
            ReportSpec.model_validate(json.loads(row["draft_spec_json"]))
            if row["draft_spec_json"]
            else None
        )

        ver_rows = self.connection.execute(
            """
            SELECT * FROM nexdash_report_versions
             WHERE key = :key
             ORDER BY version ASC
            """,
            {"key": key},
        ).fetchall()

        versions = [
            ReportVersion(
                version=int(v["version"]),
                spec=ReportSpec.model_validate(json.loads(v["spec_json"])),
                published_at=str(v["published_at"]),
                published_by=str(v["published_by"]),
                approval_code_id=str(v["approval_code_id"]),
                note=str(v["note"] or ""),
                diff_summary=str(v["diff_summary"] or ""),
                rollback_from=int(v["rollback_from"]) if v["rollback_from"] is not None else None,
            )
            for v in ver_rows
        ]

        target_for_diff = draft_spec if draft_spec is not None else published_spec
        report_diff = compute_spec_diff(published_spec, target_for_diff)

        return ReportRecord(
            key=str(row["key"]),
            published_version=int(row["published_version"]) if row["published_version"] is not None else None,
            published_spec=published_spec,
            draft_spec=draft_spec,
            draft_updated_at=str(row["draft_updated_at"]) if row["draft_updated_at"] else None,
            draft_author=str(row["draft_author"]) if row["draft_author"] else None,
            draft_note=str(row["draft_note"] or ""),
            diff=report_diff,
            versions=versions,
        )

    def list_reports(self) -> list[ReportRecord]:
        """Return all managed reports ordered by key."""
        rows = self.connection.execute("SELECT key FROM nexdash_reports ORDER BY key ASC").fetchall()
        return [self.get_report(str(r["key"])) for r in rows]

    def diff(
        self,
        key: str,
        *,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> ReportDiff:
        """Compute the diff for a report.

        By default, diffs the currently published spec against the pending draft spec.
        If `from_version` and/or `to_version` are provided, diffs those historical versions.
        """
        record = self.get_report(key)
        version_map = {v.version: v.spec for v in record.versions}

        if from_version is not None or to_version is not None:
            if from_version is not None and from_version not in version_map:
                raise ReportNotFoundError(f"Version {from_version} not found for report '{key}'")
            if to_version is not None and to_version not in version_map:
                raise ReportNotFoundError(f"Version {to_version} not found for report '{key}'")
            before_spec = version_map[from_version] if from_version is not None else record.published_spec
            after_spec = version_map[to_version] if to_version is not None else (
                record.draft_spec or record.published_spec
            )
            return compute_spec_diff(before_spec, after_spec)

        return record.diff

    # -----------------------------------------------------------------------
    # Write Operations (Require Single-Use Approval Code)
    # -----------------------------------------------------------------------

    def create_draft(
        self,
        key: str,
        spec: ReportSpec | dict[str, Any],
        approval_code: str | None,
        *,
        note: str = "",
        author: str = "agent",
        now: dt.datetime | None = None,
    ) -> ReportRecord:
        """Create a new report as a draft (does not publish until `publish` is called).

        Requires a valid `approval_code`. Validation failures do not consume the code.
        """
        try:
            validate_identifier(key, "report key")
        except ValueError as exc:
            raise ReportValidationError(str(exc)) from exc

        code_status = self._require_valid_code(approval_code, now=now)

        existing = self.connection.execute(
            "SELECT key FROM nexdash_reports WHERE key = :key",
            {"key": key},
        ).fetchone()
        if existing is not None:
            raise ReportValidationError(f"Report '{key}' already exists; use edit_draft to modify it")

        validated_spec = self._coerce_and_validate_spec(spec)
        current_time = now or _utc_now()

        assert code_status.id is not None
        self._consume_code(code_status.id, f"create_draft:{key}", now=current_time)

        self.connection.execute(
            """
            INSERT INTO nexdash_reports (
                key, published_version, published_spec_json,
                draft_spec_json, draft_updated_at, draft_author, draft_note
            ) VALUES (
                :key, NULL, NULL,
                :draft_spec_json, :draft_updated_at, :draft_author, :draft_note
            )
            """,
            {
                "key": key,
                "draft_spec_json": json.dumps(validated_spec.model_dump(mode="json")),
                "draft_updated_at": current_time.isoformat(),
                "draft_author": str(author or "agent"),
                "draft_note": str(note or ""),
            },
        )
        self.connection.commit()
        return self.get_report(key)

    def edit_draft(
        self,
        key: str,
        approval_code: str | None,
        *,
        operations: list[EditOperation | dict[str, Any]] | None = None,
        spec: ReportSpec | dict[str, Any] | None = None,
        note: str = "",
        author: str = "agent",
        now: dt.datetime | None = None,
    ) -> ReportRecord:
        """Create or update the pending draft of an existing report without modifying the published version.

        Accepts either a list of structured `operations` or a replacement `spec`.
        Requires a valid `approval_code`. Validation errors do not consume the code.
        """
        code_status = self._require_valid_code(approval_code, now=now)
        record = self.get_report(key)

        if spec is None and not operations:
            raise ReportValidationError("edit_draft requires either 'operations' or 'spec'")

        base_spec = record.draft_spec or record.published_spec
        if spec is not None:
            candidate_spec = self._coerce_and_validate_spec(spec)
        else:
            if base_spec is None:
                raise ReportValidationError(f"Report '{key}' has no base specification to apply operations to")
            assert operations is not None
            patched = apply_edit_operations(base_spec, operations)
            candidate_spec = self._coerce_and_validate_spec(patched)

        current_time = now or _utc_now()
        assert code_status.id is not None
        self._consume_code(code_status.id, f"edit_draft:{key}", now=current_time)

        self.connection.execute(
            """
            UPDATE nexdash_reports
               SET draft_spec_json = :draft_spec_json,
                   draft_updated_at = :draft_updated_at,
                   draft_author = :draft_author,
                   draft_note = :draft_note
             WHERE key = :key
            """,
            {
                "key": key,
                "draft_spec_json": json.dumps(candidate_spec.model_dump(mode="json")),
                "draft_updated_at": current_time.isoformat(),
                "draft_author": str(author or "agent"),
                "draft_note": str(note or record.draft_note),
            },
        )
        self.connection.commit()
        return self.get_report(key)

    def discard_draft(
        self,
        key: str,
        approval_code: str | None,
        *,
        now: dt.datetime | None = None,
    ) -> ReportRecord:
        """Discard the pending draft for a report while keeping any published version intact."""
        code_status = self._require_valid_code(approval_code, now=now)
        record = self.get_report(key)
        if record.draft_spec is None:
            raise ReportValidationError(f"Report '{key}' does not have a pending draft to discard")

        current_time = now or _utc_now()
        assert code_status.id is not None
        self._consume_code(code_status.id, f"discard_draft:{key}", now=current_time)

        self.connection.execute(
            """
            UPDATE nexdash_reports
               SET draft_spec_json = NULL,
                   draft_updated_at = NULL,
                   draft_author = NULL,
                   draft_note = ''
             WHERE key = :key
            """,
            {"key": key},
        )
        self.connection.commit()
        return self.get_report(key)

    def publish(
        self,
        key: str,
        approval_code: str | None,
        *,
        note: str = "",
        author: str | None = None,
        now: dt.datetime | None = None,
    ) -> ReportRecord:
        """Publish the pending draft of a report as a new immutable version.

        Requires a fresh `approval_code`. Raises `ReportValidationError` if there is no
        pending draft (without consuming the approval code).
        """
        code_status = self._require_valid_code(approval_code, now=now)
        record = self.get_report(key)

        if record.draft_spec is None:
            raise ReportValidationError(f"Report '{key}' has no pending draft to publish")

        validated_spec = self._coerce_and_validate_spec(record.draft_spec)
        diff_obj = compute_spec_diff(record.published_spec, validated_spec)

        next_version = (record.published_version or 0) + 1
        current_time = now or _utc_now()
        published_by = str(author or record.draft_author or code_status.created_by or "admin")
        publish_note = str(note or record.draft_note or "")

        assert code_status.id is not None
        self._consume_code(code_status.id, f"publish:{key}:v{next_version}", now=current_time)

        spec_json = json.dumps(validated_spec.model_dump(mode="json"))

        self.connection.execute(
            """
            UPDATE nexdash_reports
               SET published_version = :version,
                   published_spec_json = :spec_json,
                   draft_spec_json = NULL,
                   draft_updated_at = NULL,
                   draft_author = NULL,
                   draft_note = ''
             WHERE key = :key
            """,
            {
                "key": key,
                "version": next_version,
                "spec_json": spec_json,
            },
        )

        self.connection.execute(
            """
            INSERT INTO nexdash_report_versions (
                key, version, spec_json, published_at, published_by,
                approval_code_id, note, diff_summary, rollback_from
            ) VALUES (
                :key, :version, :spec_json, :published_at, :published_by,
                :approval_code_id, :note, :diff_summary, NULL
            )
            """,
            {
                "key": key,
                "version": next_version,
                "spec_json": spec_json,
                "published_at": current_time.isoformat(),
                "published_by": published_by,
                "approval_code_id": code_status.id,
                "note": publish_note,
                "diff_summary": diff_obj.summary,
            },
        )
        self.connection.commit()
        return self.get_report(key)

    def rollback(
        self,
        key: str,
        target_version: int,
        approval_code: str | None,
        *,
        note: str = "",
        author: str | None = None,
        now: dt.datetime | None = None,
    ) -> ReportRecord:
        """Roll a published report back to a previous version by creating a new published version.

        Requires a valid `approval_code`. Preserves the full version history.
        """
        code_status = self._require_valid_code(approval_code, now=now)
        record = self.get_report(key)

        target_ver_obj: ReportVersion | None = None
        for ver in record.versions:
            if ver.version == target_version:
                target_ver_obj = ver
                break

        if target_ver_obj is None:
            raise ReportNotFoundError(f"Version {target_version} not found for report '{key}'")

        if record.published_version == target_version:
            raise ReportValidationError(f"Report '{key}' is already at version {target_version}")

        restored_spec = self._coerce_and_validate_spec(target_ver_obj.spec)
        diff_obj = compute_spec_diff(record.published_spec, restored_spec)

        next_version = (record.published_version or 0) + 1
        current_time = now or _utc_now()
        published_by = str(author or code_status.created_by or "admin")
        rollback_note = str(note or f"Rollback to version {target_version}")

        assert code_status.id is not None
        self._consume_code(
            code_status.id,
            f"rollback:{key}:v{target_version}->v{next_version}",
            now=current_time,
        )

        spec_json = json.dumps(restored_spec.model_dump(mode="json"))

        self.connection.execute(
            """
            UPDATE nexdash_reports
               SET published_version = :version,
                   published_spec_json = :spec_json
             WHERE key = :key
            """,
            {
                "key": key,
                "version": next_version,
                "spec_json": spec_json,
            },
        )

        self.connection.execute(
            """
            INSERT INTO nexdash_report_versions (
                key, version, spec_json, published_at, published_by,
                approval_code_id, note, diff_summary, rollback_from
            ) VALUES (
                :key, :version, :spec_json, :published_at, :published_by,
                :approval_code_id, :note, :diff_summary, :rollback_from
            )
            """,
            {
                "key": key,
                "version": next_version,
                "spec_json": spec_json,
                "published_at": current_time.isoformat(),
                "published_by": published_by,
                "approval_code_id": code_status.id,
                "note": rollback_note,
                "diff_summary": diff_obj.summary,
                "rollback_from": target_version,
            },
        )
        self.connection.commit()
        return self.get_report(key)

    # -----------------------------------------------------------------------
    # Preview (Read-Only Execution of Draft, Published, or Ephemeral Spec)
    # -----------------------------------------------------------------------

    def preview(
        self,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        operations: list[EditOperation | dict[str, Any]] | None = None,
        spec: ReportSpec | dict[str, Any] | None = None,
        executor: SQLiteExecutor | None = None,
        registry: CubeRegistry | None = None,
        sample_limit: int = DEFAULT_PREVIEW_SAMPLE_LIMIT,
        include_narrative: bool = False,
        narrative_config: Any | None = None,
    ) -> ReportPreview:
        """Execute a report against the semantic layer and return a full preview without saving changes.

        Does not require an approval code and never mutates stored report state.
        """
        active_registry = registry or self.registry
        active_executor = executor or self.executor
        if active_registry is None:
            raise ReportValidationError("preview requires a CubeRegistry (either on ReportStore or passed to preview)")
        if active_executor is None:
            raise ReportValidationError(
                "preview requires a SQLiteExecutor (either on ReportStore or passed to preview)"
            )

        source_state: PreviewSourceState
        if spec is not None:
            validate_identifier(key, "report key")
            target_spec = self._coerce_and_validate_spec(spec)
            source_state = "ephemeral"
        else:
            record = self.get_report(key)
            base_spec = record.draft_spec or record.published_spec
            if base_spec is None:
                raise ReportValidationError(f"Report '{key}' has no specification to preview")
            if operations:
                target_spec = self._coerce_and_validate_spec(apply_edit_operations(base_spec, operations))
                source_state = "ephemeral"
            else:
                target_spec = base_spec
                source_state = "draft" if record.draft_spec is not None else "published"

        effective_plan = bind_runtime_parameters(target_spec, params)
        if effective_plan.cube not in active_registry:
            raise ReportValidationError(f"Cube '{effective_plan.cube}' not found in registry")
        cube = active_registry.get(effective_plan.cube)

        try:
            compiled = self.compiler.compile(
                effective_plan,
                cube,
                registry=active_registry,
                dialect="sqlite",
            )
        except (AllowlistError, CompilationError) as exc:
            raise ReportValidationError(f"Preview query compilation failed: {exc}") from exc

        exec_result = active_executor.execute_compiled(compiled)
        sample_rows = exec_result.rows[: max(1, sample_limit)]
        charts, warnings = compute_charts_from_rows(target_spec, exec_result.columns, exec_result.rows)

        if exec_result.row_count == 0:
            warnings.append("Query returned 0 rows for the current filter parameters.")

        narrative_text: str | None = None
        if include_narrative:
            narrative_text = generate_report_narrative(
                target_spec,
                sample_rows,
                config=narrative_config,
            )

        return ReportPreview(
            key=key,
            source_state=source_state,
            spec=target_spec,
            compiled_sql=compiled.sql,
            sql_params=compiled.params,
            columns=exec_result.columns,
            rows=sample_rows,
            total_rows=exec_result.row_count,
            charts=charts,
            warnings=warnings,
            narrative=narrative_text,
        )
