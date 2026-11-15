"""Integration tests demonstrating end-to-end semantic layer workflow."""

from pathlib import Path

from nexdash import (
    CubeRegistry,
    PlanFilter,
    PlanOrderBy,
    QueryPlan,
    SQLiteExecutor,
    TimeRange,
)


def test_end_to_end_semantic_pipeline():
    # 1. Load cubes from examples directory
    cubes_dir = Path(__file__).parent.parent / "examples" / "cubes"
    registry = CubeRegistry.from_directory(cubes_dir)

    assert "orders" in registry
    assert "customers" in registry

    orders_cube = registry.get("orders")
    assert orders_cube.name == "orders"

    # 2. Setup SQLite database with synthetic data
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
            ('c2', 'Florianopolis', 'silver');

        INSERT INTO fact_orders VALUES
            ('o1', 'c1', 'completed', 120.0, '2026-05-01'),
            ('o2', 'c1', 'completed', 180.0, '2026-05-03'),
            ('o3', 'c2', 'completed', 250.0, '2026-05-15'),
            ('o4', 'c2', 'cancelled', 70.0, '2026-05-20');
        """
    )

    # 3. Create query plan
    plan = QueryPlan(
        cube="orders",
        dimensions=["status"],
        metrics=["order_count", "total_revenue", "avg_order_value"],
        filters=[
            PlanFilter(field="status", operator="eq", value="completed"),
        ],
        time_range=TimeRange(field="order_date", start="2026-05-01", end="2026-05-31"),
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    )

    # 4. Execute plan
    result = executor.execute_plan(plan, orders_cube, registry=registry)

    # 5. Verify results
    assert len(result) == 1
    row = result[0]
    assert row["status"] == "completed"
    assert row["order_count"] == 3
    assert row["total_revenue"] == 550.0
    assert row["avg_order_value"] == pytest_approx(183.333, abs=0.01)

    # 6. Test join query
    join_plan = QueryPlan(
        cube="orders",
        dimensions=["customers.city"],
        metrics=["total_revenue"],
        joins=["customers"],
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    )
    join_result = executor.execute_plan(join_plan, orders_cube, registry=registry)
    assert len(join_result) == 2
    # Florianopolis: o3 (250) + o4 (70) = 320
    # Porto Alegre: o1 (120) + o2 (180) = 300
    assert join_result[0]["customers.city"] == "Florianopolis"
    assert join_result[0]["total_revenue"] == 320.0
    assert join_result[1]["customers.city"] == "Porto Alegre"
    assert join_result[1]["total_revenue"] == 300.0

    executor.close()


def test_end_to_end_with_database_executor():
    # End-to-end pipeline running on pluggable DatabaseExecutor (SQLite or DB_URL if set)
    cubes_dir = Path(__file__).parent.parent / "examples" / "cubes"
    registry = CubeRegistry.from_directory(cubes_dir)
    orders_cube = registry.get("orders")

    from nexdash import DatabaseExecutor, get_db_url

    db_url = get_db_url()
    executor = DatabaseExecutor(db_url)

    # Clean setup and DDL compatible with SQLite, PostgreSQL, and MySQL
    executor.execute_script(
        """
        DROP TABLE IF EXISTS fact_orders;
        DROP TABLE IF EXISTS dim_customers;

        CREATE TABLE dim_customers (
            id VARCHAR(64) PRIMARY KEY,
            city VARCHAR(64),
            tier VARCHAR(64)
        );

        CREATE TABLE fact_orders (
            order_id VARCHAR(64) PRIMARY KEY,
            customer_id VARCHAR(64),
            status VARCHAR(64),
            amount NUMERIC,
            order_date VARCHAR(64)
        );

        INSERT INTO dim_customers (id, city, tier) VALUES
            ('c1', 'Porto Alegre', 'gold'),
            ('c2', 'Florianopolis', 'silver');

        INSERT INTO fact_orders (order_id, customer_id, status, amount, order_date) VALUES
            ('o1', 'c1', 'completed', 120.0, '2026-05-01'),
            ('o2', 'c1', 'completed', 180.0, '2026-05-03'),
            ('o3', 'c2', 'completed', 250.0, '2026-05-15'),
            ('o4', 'c2', 'cancelled', 70.0, '2026-05-20');
        """
    )

    plan = QueryPlan(
        cube="orders",
        dimensions=["status"],
        metrics=["order_count", "total_revenue"],
        filters=[PlanFilter(field="status", operator="eq", value="completed")],
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    )

    result = executor.execute_plan(plan, orders_cube, registry=registry)
    assert len(result) == 1
    assert result[0]["status"] == "completed"
    assert result[0]["order_count"] == 3
    assert float(result[0]["total_revenue"]) == 550.0

    executor.close()


def pytest_approx(val, abs=1e-4):
    from pytest import approx

    return approx(val, abs=abs)
