"""Tests for the SQL compiler and allowlist enforcement."""

import pytest

from nexdash.compiler import AllowlistError, SQLCompiler
from nexdash.cube import Cube, Dimension, FilterRule, Join, Metric
from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan, TimeRange


@pytest.fixture
def sample_cube() -> Cube:
    return Cube(
        name="orders",
        source="fact_orders",
        dimensions=[
            Dimension(name="status", type="string"),
            Dimension(name="region", column="geo_region", type="string"),
            Dimension(name="order_date", type="date"),
        ],
        metrics=[
            Metric(name="order_count", aggregation="count"),
            Metric(name="total_revenue", aggregation="sum", column="amount"),
            Metric(name="avg_revenue", aggregation="avg", column="amount"),
        ],
        joins=[
            Join(
                name="customers",
                target_table="dim_customers",
                source_key="customer_id",
                target_key="id",
            )
        ],
        filters=[
            FilterRule(name="status", operators=["eq", "neq", "in"]),
            FilterRule(name="order_date"),
        ],
    )


def test_valid_plan_compiles(sample_cube: Cube):
    compiler = SQLCompiler()
    plan = QueryPlan(
        cube="orders",
        metrics=["total_revenue", "order_count"],
        dimensions=["status"],
        filters=[
            PlanFilter(field="status", operator="eq", value="completed"),
        ],
        time_range=TimeRange(field="order_date", start="2026-01-01", end="2026-12-31"),
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
        limit=10,
    )

    compiled = compiler.compile(plan, sample_cube)

    assert "SELECT fact_orders.status AS status" in compiled.sql
    assert "sum(fact_orders.amount) AS total_revenue" in compiled.sql
    assert "count(*) AS order_count" in compiled.sql
    assert "FROM fact_orders" in compiled.sql
    assert "GROUP BY fact_orders.status" in compiled.sql
    assert "ORDER BY sum(fact_orders.amount) DESC" in compiled.sql or "ORDER BY total_revenue DESC" in compiled.sql
    assert "LIMIT" in compiled.sql

    # Parameters must contain the values
    assert compiled.params["p_1_status"] == "completed"
    assert compiled.params["tr_start_2"] == "2026-01-01"
    assert compiled.params["tr_end_2"] == "2026-12-31"


def test_malicious_sql_injection_value_is_parameterized(sample_cube: Cube):
    compiler = SQLCompiler()
    malicious_value = "completed'; DROP TABLE fact_orders; --"
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count"],
        filters=[
            PlanFilter(field="status", operator="eq", value=malicious_value),
        ],
    )

    compiled = compiler.compile(plan, sample_cube)

    # The SQL string must NOT contain DROP TABLE
    assert "DROP TABLE" not in compiled.sql
    assert "DROP TABLE" not in compiled.sql.upper()
    assert malicious_value not in compiled.sql

    # The value is safely preserved in the params dictionary
    assert compiled.params["p_1_status"] == malicious_value


def test_dimension_not_in_allowlist_rejected(sample_cube: Cube):
    compiler = SQLCompiler()
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count"],
        dimensions=["unapproved_secret_dim"],
    )

    with pytest.raises(AllowlistError) as exc:
        compiler.compile(plan, sample_cube)
    assert "not declared in cube" in str(exc.value)


def test_metric_not_in_allowlist_rejected(sample_cube: Cube):
    compiler = SQLCompiler()
    plan = QueryPlan(
        cube="orders",
        metrics=["unapproved_metric"],
    )

    with pytest.raises(AllowlistError) as exc:
        compiler.compile(plan, sample_cube)
    assert "not declared in cube" in str(exc.value)


def test_join_not_in_allowlist_rejected(sample_cube: Cube):
    compiler = SQLCompiler()
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count"],
        joins=["unapproved_join"],
    )

    with pytest.raises(AllowlistError) as exc:
        compiler.compile(plan, sample_cube)
    assert "not declared in cube" in str(exc.value)


def test_filter_field_not_in_allowlist_rejected(sample_cube: Cube):
    compiler = SQLCompiler()
    # region is a dimension, but cube explicitly declared filters only for status and order_date
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count"],
        filters=[PlanFilter(field="region", operator="eq", value="South")],
    )

    with pytest.raises(AllowlistError) as exc:
        compiler.compile(plan, sample_cube)
    assert "not in allowlist" in str(exc.value)


def test_filter_operator_not_in_allowlist_rejected(sample_cube: Cube):
    compiler = SQLCompiler()
    # status filter only allows eq, neq, in
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count"],
        filters=[PlanFilter(field="status", operator="gt", value="abc")],
    )

    with pytest.raises(AllowlistError) as exc:
        compiler.compile(plan, sample_cube)
    assert "not in allowlist" in str(exc.value)


def test_cube_name_mismatch_rejected(sample_cube: Cube):
    compiler = SQLCompiler()
    plan = QueryPlan(
        cube="different_cube",
        metrics=["order_count"],
    )

    with pytest.raises(AllowlistError) as exc:
        compiler.compile(plan, sample_cube)
    assert "does not match target cube" in str(exc.value)


def test_join_compilation(sample_cube: Cube):
    compiler = SQLCompiler()
    plan = QueryPlan(
        cube="orders",
        metrics=["total_revenue"],
        dimensions=["customers.city"],
        joins=["customers"],
    )

    compiled = compiler.compile(plan, sample_cube)
    assert "JOIN dim_customers ON fact_orders.customer_id = dim_customers.id" in compiled.sql
    assert "dim_customers.city AS \"customers.city\"" in compiled.sql or "dim_customers.city" in compiled.sql
