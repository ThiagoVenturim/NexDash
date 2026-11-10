"""Tests for semantic cube models and YAML loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from askdash.cube import (
    Cube,
    CubeRegistry,
    Dimension,
    FilterRule,
    Join,
    Metric,
    load_cube_from_yaml,
)


def test_valid_cube_instantiation():
    cube = Cube(
        name="orders",
        source="fact_orders",
        dimensions=[
            Dimension(name="order_id", type="string"),
            Dimension(name="status", type="string"),
            Dimension(name="order_date", type="date"),
        ],
        metrics=[
            Metric(name="order_count", aggregation="count"),
            Metric(name="total_amount", aggregation="sum", column="amount"),
            Metric(name="avg_amount", aggregation="avg", column="amount"),
        ],
        joins=[
            Join(
                name="customers",
                source_key="customer_id",
                target_key="id",
                target_table="dim_customers",
            )
        ],
        filters=[
            FilterRule(name="status", operators=["eq", "in"]),
            FilterRule(name="order_date"),
        ],
    )
    assert cube.name == "orders"
    assert cube.source == "fact_orders"
    assert cube.allowed_dimension_names == {"order_id", "status", "order_date"}
    assert cube.allowed_metric_names == {"order_count", "total_amount", "avg_amount"}
    assert cube.allowed_join_names == {"customers"}
    assert cube.allowed_filter_names == {"status", "order_date"}
    assert cube.is_filter_allowed("status", "eq") is True
    assert cube.is_filter_allowed("status", "gt") is False
    assert cube.is_filter_allowed("order_date", "gte") is True
    assert cube.is_filter_allowed("unknown_col", "eq") is False


def test_metric_sum_without_column_rejected():
    with pytest.raises(ValidationError) as exc:
        Cube(
            name="orders",
            source="orders",
            metrics=[Metric(name="invalid_sum", aggregation="sum")],
        )
    assert "requires a column" in str(exc.value)


def test_invalid_aggregation_rejected():
    with pytest.raises(ValidationError):
        Cube(
            name="orders",
            source="orders",
            metrics=[Metric(name="invalid", aggregation="median", column="amount")],  # type: ignore[arg-type]
        )


def test_duplicate_dimension_rejected():
    with pytest.raises(ValidationError) as exc:
        Cube(
            name="orders",
            source="orders",
            dimensions=[
                Dimension(name="status"),
                Dimension(name="status"),
            ],
        )
    assert "Duplicate dimension name 'status'" in str(exc.value)


def test_metric_dimension_collision_rejected():
    with pytest.raises(ValidationError) as exc:
        Cube(
            name="orders",
            source="orders",
            dimensions=[Dimension(name="revenue")],
            metrics=[Metric(name="revenue", aggregation="sum", column="amount")],
        )
    assert "conflicts with dimension name" in str(exc.value)


def test_sql_injection_in_identifiers_rejected():
    with pytest.raises(ValidationError):
        Cube(name="orders; drop table users;", source="orders")

    with pytest.raises(ValidationError):
        Dimension(name="status--comment")

    with pytest.raises(ValidationError):
        Join(name="valid", source_key="id; drop", target_key="id")


def test_load_cube_from_yaml_valid(tmp_path: Path):
    yaml_content = """
name: sales
source: mart_sales
description: Sales transactions cube
dimensions:
  - name: region
    column: store_region
    type: string
  - name: product_category
    type: string
metrics:
  - name: total_sales
    aggregation: sum
    column: amount
  - name: transaction_count
    aggregation: count
joins:
  - name: stores
    source_key: store_id
    target_key: id
filters:
  - region
  - product_category
"""
    cube_file = tmp_path / "sales.yaml"
    cube_file.write_text(yaml_content, encoding="utf-8")

    cube = load_cube_from_yaml(cube_file)
    assert cube.name == "sales"
    assert cube.source == "mart_sales"
    assert len(cube.dimensions) == 2
    assert cube.get_dimension("region").column == "store_region"
    assert cube.get_dimension("product_category").column == "product_category"
    assert len(cube.metrics) == 2
    assert cube.allowed_filter_names == {"region", "product_category"}


def test_load_cubes_from_directory(tmp_path: Path):
    cube1 = """
name: orders
source: orders_tbl
dimensions:
  - name: status
metrics:
  - name: count
    aggregation: count
"""
    cube2 = """
name: customers
source: customers_tbl
dimensions:
  - name: tier
metrics:
  - name: count
    aggregation: count
"""
    (tmp_path / "orders.yaml").write_text(cube1, encoding="utf-8")
    (tmp_path / "customers.yml").write_text(cube2, encoding="utf-8")

    registry = CubeRegistry.from_directory(tmp_path)
    assert registry.list_cubes() == ["customers", "orders"]
    assert registry.get("orders").source == "orders_tbl"

    with pytest.raises(KeyError):
        registry.get("nonexistent")
