"""Tests for the managed reports lifecycle: draft, preview, diff, publish, rollback, and approval codes."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from nexcoder_core import NexcoderConfig

from nexdash import (
    ApprovalCodeError,
    CubeRegistry,
    EditOperation,
    PlanOrderBy,
    QueryPlan,
    ReportChart,
    ReportColumn,
    ReportNotFoundError,
    ReportParameter,
    ReportSpec,
    ReportStore,
    ReportValidationError,
    SQLiteExecutor,
    TimeRange,
    apply_edit_operations,
    compute_spec_diff,
    hash_approval_code,
    normalize_approval_code,
)


@pytest.fixture()
def semantic_env() -> tuple[CubeRegistry, SQLiteExecutor]:
    cubes_dir = Path(__file__).parent.parent / "examples" / "cubes"
    registry = CubeRegistry.from_directory(cubes_dir)

    executor = SQLiteExecutor(":memory:")
    executor.execute_script(
        """
        CREATE TABLE fact_orders (
            order_id TEXT PRIMARY KEY,
            customer_id TEXT,
            status TEXT,
            amount REAL,
            order_date TEXT
        );

        CREATE TABLE dim_customers (
            id TEXT PRIMARY KEY,
            city TEXT,
            tier TEXT
        );

        INSERT INTO dim_customers VALUES
            ('c1', 'Porto Alegre', 'gold'),
            ('c2', 'Florianopolis', 'silver'),
            ('c3', 'Curitiba', 'gold');

        INSERT INTO fact_orders VALUES
            ('o1', 'c1', 'completed', 120.0, '2026-05-01'),
            ('o2', 'c1', 'completed', 180.0, '2026-05-03'),
            ('o3', 'c2', 'completed', 250.0, '2026-05-15'),
            ('o4', 'c2', 'cancelled', 70.0, '2026-05-20'),
            ('o5', 'c3', 'completed', 400.0, '2026-05-22');
        """
    )
    yield registry, executor
    executor.close()


def _sample_spec(title: str = "Orders by Status") -> ReportSpec:
    return ReportSpec(
        title=title,
        description="Summary of orders grouped by status.",
        plan=QueryPlan(
            cube="orders",
            dimensions=["status"],
            metrics=["order_count", "total_revenue"],
            order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
            limit=25,
        ),
        parameters=[
            ReportParameter(
                name="status_filter",
                field="status",
                operator="eq",
                default=None,
                description="Optional order status filter",
            )
        ],
        columns=[
            ReportColumn(field="status", label="Status"),
            ReportColumn(field="order_count", label="Orders"),
            ReportColumn(field="total_revenue", label="Revenue", format="currency"),
        ],
        charts=[
            ReportChart(
                id="revenue_by_status",
                type="bar",
                title="Revenue by Status",
                x="status",
                y="total_revenue",
            ),
            ReportChart(
                id="top_revenue_kpi",
                type="kpi",
                title="Top Status Revenue",
                y="total_revenue",
            ),
            ReportChart(
                id="summary_table",
                type="table",
                title="Orders Table",
            ),
        ],
        tags=["sales", "executive"],
    )


def test_approval_code_generation_normalization_and_single_use(semantic_env):
    registry, executor = semantic_env
    with ReportStore(":memory:", registry=registry, executor=executor) as store:
        issued = store.generate_approval_code(created_by="admin_user", ttl_minutes=10)
        assert len(normalize_approval_code(issued.code)) == 8
        assert "-" in issued.code
        assert issued.created_by == "admin_user"

        # Case-insensitive and whitespace/hyphen-tolerant verification
        spaced_lower = issued.code.replace("-", " ").lower()
        assert normalize_approval_code(spaced_lower) == normalize_approval_code(issued.code)
        assert hash_approval_code(spaced_lower) == hash_approval_code(issued.code)

        status = store.verify_approval_code(spaced_lower)
        assert status.valid is True
        assert status.created_by == "admin_user"

        # Use once to create a draft
        record = store.create_draft("orders_summary", _sample_spec(), spaced_lower, note="Initial draft")
        assert record.has_draft is True
        assert record.is_published is False

        # Reusing the same approval code must fail
        after_use = store.verify_approval_code(issued.code)
        assert after_use.valid is False
        assert "already been used" in (after_use.reason or "")

        with pytest.raises(ApprovalCodeError, match="already been used"):
            store.publish("orders_summary", issued.code)


def test_approval_code_expiry_and_unknown_rejection(semantic_env):
    registry, executor = semantic_env
    with ReportStore(":memory:", registry=registry, executor=executor) as store:
        t0 = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=dt.UTC)
        issued = store.generate_approval_code(created_by="admin_user", ttl_minutes=5, now=t0)

        # Expired 6 minutes later
        t_expired = t0 + dt.timedelta(minutes=6)
        status = store.verify_approval_code(issued.code, now=t_expired)
        assert status.valid is False
        assert "expired" in (status.reason or "").lower()

        with pytest.raises(ApprovalCodeError, match="expired"):
            store.create_draft("orders_summary", _sample_spec(), issued.code, now=t_expired)

        # Missing, short, or unknown codes are rejected on every write operation
        for invalid_code in (None, "", "   ", "SHORT", "ZZZZ-ZZZZ"):
            with pytest.raises(ApprovalCodeError):
                store.create_draft("orders_summary", _sample_spec(), invalid_code)


def test_validation_error_does_not_consume_approval_code(semantic_env):
    registry, executor = semantic_env
    with ReportStore(":memory:", registry=registry, executor=executor) as store:
        code = store.generate_approval_code(created_by="reviewer").code

        # 1. Invalid report key does not consume the code
        with pytest.raises(ReportValidationError, match="Invalid report key"):
            store.create_draft("123-bad-key!", _sample_spec(), code)
        assert store.verify_approval_code(code).valid is True

        # 2. Invalid cube metric (blocked by semantic allowlist) does not consume the code
        bad_spec_dict = {
            "title": "Bad Metric Report",
            "plan": {
                "cube": "orders",
                "dimensions": ["status"],
                "metrics": ["non_existent_metric"],
            },
        }
        with pytest.raises(ReportValidationError, match="semantic guard validation"):
            store.create_draft("orders_summary", bad_spec_dict, code)
        assert store.verify_approval_code(code).valid is True

        # 3. Retry with valid spec and the SAME approval code succeeds and consumes it
        record = store.create_draft("orders_summary", _sample_spec(), code)
        assert record.key == "orders_summary"
        assert store.verify_approval_code(code).valid is False


def test_full_lifecycle_draft_preview_diff_publish_and_rollback(semantic_env):
    registry, executor = semantic_env
    with ReportStore(":memory:", registry=registry, executor=executor) as store:
        # Step 1: Create draft
        c1 = store.generate_approval_code("alice").code
        draft_rec = store.create_draft(
            "orders_report",
            _sample_spec("Orders Report v1"),
            c1,
            note="First draft",
            author="alice",
        )
        assert draft_rec.has_draft is True
        assert draft_rec.is_published is False
        assert draft_rec.published_version is None
        assert draft_rec.diff.is_initial is True
        assert draft_rec.diff.has_changes is True

        # Step 2: Preview the draft (no approval code required, SQL is parameterized)
        prev1 = store.preview("orders_report", params={"status_filter": "completed"})
        assert prev1.source_state == "draft"
        assert prev1.total_rows == 1
        assert prev1.rows[0]["status"] == "completed"
        assert prev1.rows[0]["order_count"] == 4
        assert prev1.rows[0]["total_revenue"] == 950.0
        # Verify filter value is bound as a parameter, never interpolated in SQL string
        assert "completed" not in prev1.compiled_sql
        assert "completed" in prev1.sql_params.values()
        # Verify deterministic chart calculation
        bar_chart = next(c for c in prev1.charts if c.id == "revenue_by_status")
        assert bar_chart.labels == ["completed"]
        assert bar_chart.values == [950.0]
        kpi_chart = next(c for c in prev1.charts if c.id == "top_revenue_kpi")
        assert kpi_chart.kpi_value == 950.0

        # Step 3: Publishing without an approval code MUST fail
        with pytest.raises(ApprovalCodeError):
            store.publish("orders_report", None)
        with pytest.raises(ApprovalCodeError):
            store.publish("orders_report", "")

        # Step 4: Publish v1 with a fresh approval code
        c2 = store.generate_approval_code("alice").code
        pub_v1 = store.publish("orders_report", c2, note="Publish v1")
        assert pub_v1.is_published is True
        assert pub_v1.has_draft is False
        assert pub_v1.published_version == 1
        assert pub_v1.published_spec is not None
        assert pub_v1.published_spec.title == "Orders Report v1"
        assert len(pub_v1.versions) == 1
        assert pub_v1.versions[0].version == 1
        assert pub_v1.diff.has_changes is False

        # Publishing again when no draft exists fails without consuming the code
        c_unused = store.generate_approval_code("alice").code
        with pytest.raises(ReportValidationError, match="no pending draft"):
            store.publish("orders_report", c_unused)
        assert store.verify_approval_code(c_unused).valid is True

        # Step 5: Edit draft using structured operations (published v1 stays untouched)
        ops = [
            EditOperation(op="set", path="title", value="Orders Report v2 (With Avg)"),
            EditOperation(op="add", path="plan.metrics", value="avg_order_value"),
            EditOperation(
                op="update",
                path="charts",
                id="revenue_by_status",
                value={"type": "line", "title": "Revenue Trend"},
            ),
        ]
        edited_rec = store.edit_draft("orders_report", c_unused, operations=ops, note="Add avg metric")
        assert edited_rec.is_published is True
        assert edited_rec.published_version == 1
        assert edited_rec.published_spec is not None
        assert edited_rec.published_spec.title == "Orders Report v1"
        assert edited_rec.has_draft is True
        assert edited_rec.draft_spec is not None
        assert edited_rec.draft_spec.title == "Orders Report v2 (With Avg)"
        assert "avg_order_value" in edited_rec.draft_spec.plan.metrics

        # Step 6: Inspect diff between published v1 and pending draft
        diff_v1_draft = store.diff("orders_report")
        assert diff_v1_draft.has_changes is True
        assert diff_v1_draft.is_initial is False
        changed_paths = {entry.path for entry in diff_v1_draft.changes}
        assert "title" in changed_paths
        assert "plan.metrics" in changed_paths
        assert "charts" in changed_paths
        assert "--- published" in diff_v1_draft.unified_diff
        assert "+++ draft" in diff_v1_draft.unified_diff

        # Step 7: Publish v2
        c3 = store.generate_approval_code("bob").code
        pub_v2 = store.publish("orders_report", c3, note="Publish v2", author="bob")
        assert pub_v2.published_version == 2
        assert pub_v2.has_draft is False
        assert pub_v2.published_spec is not None
        assert pub_v2.published_spec.title == "Orders Report v2 (With Avg)"
        assert len(pub_v2.versions) == 2

        # Historical diff between v1 and v2
        hist_diff = store.diff("orders_report", from_version=1, to_version=2)
        assert hist_diff.has_changes is True
        assert any(e.path == "title" and e.before == "Orders Report v1" for e in hist_diff.changes)

        # Step 8: Rollback from v2 to v1 (requires approval code, creates v3)
        with pytest.raises(ApprovalCodeError):
            store.rollback("orders_report", 1, None)

        c4 = store.generate_approval_code("alice").code
        # Rolling back to current version (2) fails without consuming code
        with pytest.raises(ReportValidationError, match="already at version 2"):
            store.rollback("orders_report", 2, c4)
        assert store.verify_approval_code(c4).valid is True

        # Rolling back to non-existent version (99) fails without consuming code
        with pytest.raises(ReportNotFoundError, match="Version 99 not found"):
            store.rollback("orders_report", 99, c4)
        assert store.verify_approval_code(c4).valid is True

        rolled_back = store.rollback("orders_report", 1, c4, note="Revert to v1")
        assert rolled_back.published_version == 3
        assert rolled_back.published_spec is not None
        assert rolled_back.published_spec.title == "Orders Report v1"
        assert rolled_back.published_spec.plan.metrics == ["order_count", "total_revenue"]
        assert len(rolled_back.versions) == 3
        assert rolled_back.versions[2].version == 3
        assert rolled_back.versions[2].rollback_from == 1


def test_discard_draft_and_ephemeral_preview_with_offline_narrative(semantic_env):
    registry, executor = semantic_env
    with ReportStore(":memory:", registry=registry, executor=executor) as store:
        c1 = store.generate_approval_code("admin").code
        store.create_draft("city_orders", _sample_spec("Orders v1"), c1)
        c2 = store.generate_approval_code("admin").code
        store.publish("city_orders", c2)

        # Create a new draft
        c3 = store.generate_approval_code("admin").code
        store.edit_draft(
            "city_orders",
            c3,
            operations=[{"op": "set", "path": "title", "value": "Temporary Draft Title"}],
        )
        assert store.get_report("city_orders").has_draft is True

        # Preview with ephemeral operations and offline narrative via nexcoder-core
        ephemeral_preview = store.preview(
            "city_orders",
            operations=[
                {"op": "set", "path": "plan.dimensions", "value": ["customers.city"]},
                {"op": "set", "path": "plan.joins", "value": ["customers"]},
                {"op": "set", "path": "charts", "value": []},
                {"op": "set", "path": "columns", "value": []},
            ],
            params={"time_range": TimeRange(field="order_date", start="2026-05-01", end="2026-05-31")},
            include_narrative=True,
            narrative_config=NexcoderConfig(preset="offline"),
        )
        assert ephemeral_preview.source_state == "ephemeral"
        assert ephemeral_preview.total_rows == 3
        assert ephemeral_preview.narrative is not None
        assert len(ephemeral_preview.narrative) > 0

        # Discard the pending draft (requires approval code)
        with pytest.raises(ApprovalCodeError):
            store.discard_draft("city_orders", None)

        c4 = store.generate_approval_code("admin").code
        after_discard = store.discard_draft("city_orders", c4)
        assert after_discard.has_draft is False
        assert after_discard.is_published is True
        assert after_discard.published_spec is not None
        assert after_discard.published_spec.title == "Orders v1"


def test_edit_operations_move_remove_unset_and_diff_helpers():
    base = _sample_spec("Base Title")
    ops = [
        {"op": "add", "path": "tags", "value": "urgent", "before": "sales"},
        {"op": "move", "path": "charts", "id": "summary_table", "position": 0},
        {"op": "remove", "path": "charts", "id": "top_revenue_kpi"},
        {"op": "unset", "path": "description"},
    ]
    updated = apply_edit_operations(base, ops)
    assert updated.tags == ["urgent", "sales", "executive"]
    assert [c.id for c in updated.charts] == ["summary_table", "revenue_by_status"]
    assert updated.description is None

    diff = compute_spec_diff(base, updated)
    assert diff.has_changes is True
    assert "Modified:" in diff.summary
