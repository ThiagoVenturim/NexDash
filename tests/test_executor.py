"""Tests for the SQLite executor on synthetic analytical data."""

import pytest

from nexdash.cube import Cube, Dimension, FilterRule, Join, Metric
from nexdash.executor import SQLiteExecutor
from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan, TimeRange


@pytest.fixture
def executor() -> SQLiteExecutor:
    exec_instance = SQLiteExecutor(":memory:")
    # Seed synthetic schema and data
    exec_instance.execute_script(
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
            ('c1', 'Curitiba', 'gold'),
            ('c2', 'Sao Paulo', 'silver'),
            ('c3', 'Curitiba', 'bronze');

        INSERT INTO fact_orders VALUES
            ('o1', 'c1', 'completed', 100.0, '2026-03-01'),
            ('o2', 'c1', 'completed', 150.0, '2026-03-05'),
            ('o3', 'c2', 'completed', 200.0, '2026-03-10'),
            ('o4', 'c2', 'cancelled', 50.0, '2026-03-12'),
            ('o5', 'c3', 'completed', 300.0, '2026-04-01');
        """
    )
    yield exec_instance
    exec_instance.close()


@pytest.fixture
def orders_cube() -> Cube:
    return Cube(
        name="orders",
        source="fact_orders",
        dimensions=[
            Dimension(name="status", type="string"),
            Dimension(name="order_date", type="date"),
            Dimension(name="order_id", type="string"),
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
            FilterRule(name="status"),
            FilterRule(name="order_date"),
        ],
    )


def test_aggregate_query_execution(executor: SQLiteExecutor, orders_cube: Cube):
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count", "total_revenue", "avg_revenue"],
    )

    result = executor.execute_plan(plan, orders_cube)
    assert len(result) == 1
    row = result[0]
    assert row["order_count"] == 5
    assert row["total_revenue"] == 800.0
    assert row["avg_revenue"] == 160.0


def test_grouped_query_with_filter_and_order(executor: SQLiteExecutor, orders_cube: Cube):
    plan = QueryPlan(
        cube="orders",
        dimensions=["status"],
        metrics=["total_revenue", "order_count"],
        filters=[
            PlanFilter(field="status", operator="eq", value="completed"),
        ],
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    )

    result = executor.execute_plan(plan, orders_cube)
    assert len(result) == 1
    assert result[0]["status"] == "completed"
    assert result[0]["total_revenue"] == 750.0
    assert result[0]["order_count"] == 4


def test_time_range_filter_execution(executor: SQLiteExecutor, orders_cube: Cube):
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count", "total_revenue"],
        time_range=TimeRange(field="order_date", start="2026-03-01", end="2026-03-10"),
    )

    result = executor.execute_plan(plan, orders_cube)
    assert len(result) == 1
    assert result[0]["order_count"] == 3
    assert result[0]["total_revenue"] == 450.0


def test_joined_query_execution(executor: SQLiteExecutor, orders_cube: Cube):
    plan = QueryPlan(
        cube="orders",
        dimensions=["customers.city"],
        metrics=["total_revenue"],
        joins=["customers"],
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    )

    result = executor.execute_plan(plan, orders_cube)
    assert len(result) == 2
    # Curitiba has o1 (100) + o2 (150) + o5 (300) = 550
    # Sao Paulo has o3 (200) + o4 (50) = 250
    assert result[0]["customers.city"] == "Curitiba"
    assert result[0]["total_revenue"] == 550.0
    assert result[1]["customers.city"] == "Sao Paulo"
    assert result[1]["total_revenue"] == 250.0


def test_sql_injection_defense(executor: SQLiteExecutor, orders_cube: Cube):
    # Pass a malicious SQL injection attempt as filter value
    malicious_val = "completed'; DROP TABLE fact_orders; --"
    plan = QueryPlan(
        cube="orders",
        metrics=["order_count"],
        filters=[
            PlanFilter(field="status", operator="eq", value=malicious_val),
        ],
    )

    result = executor.execute_plan(plan, orders_cube)
    # The condition is treated literally, matching 0 rows
    assert len(result) == 1
    assert result[0]["order_count"] == 0

    # Ensure table fact_orders was NOT dropped and still intact
    check_plan = QueryPlan(cube="orders", metrics=["order_count"])
    check_result = executor.execute_plan(check_plan, orders_cube)
    assert check_result[0]["order_count"] == 5
