"""Tests for structured query plan models and validations."""

import pytest
from pydantic import ValidationError

from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan, TimeRange


def test_valid_query_plan():
    plan = QueryPlan(
        cube="orders",
        metrics=["total_revenue", "order_count"],
        dimensions=["status", "region"],
        filters=[
            PlanFilter(field="status", operator="eq", value="completed"),
            PlanFilter(field="total_revenue", operator="gt", value=100.5),
            PlanFilter(field="region", operator="in", value=["North", "South"]),
        ],
        time_range=TimeRange(field="order_date", start="2026-01-01", end="2026-12-31"),
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
        limit=50,
        offset=10,
    )
    assert plan.cube == "orders"
    assert len(plan.metrics) == 2
    assert len(plan.dimensions) == 2
    assert len(plan.filters) == 3
    assert plan.limit == 50
    assert plan.offset == 10


def test_empty_plan_rejected():
    with pytest.raises(ValidationError) as exc:
        QueryPlan(cube="orders")
    assert "must request at least one metric or dimension" in str(exc.value)


def test_duplicate_fields_in_plan_rejected():
    with pytest.raises(ValidationError) as exc:
        QueryPlan(cube="orders", metrics=["total_revenue", "total_revenue"])
    assert "Duplicate metric" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        QueryPlan(cube="orders", dimensions=["status", "status"])
    assert "Duplicate dimension" in str(exc.value)


def test_sql_injection_in_plan_fields_rejected():
    with pytest.raises(ValidationError):
        QueryPlan(cube="orders; DROP TABLE users;", metrics=["count"])

    with pytest.raises(ValidationError):
        QueryPlan(cube="orders", metrics=["count() AS evil"])

    with pytest.raises(ValidationError):
        PlanFilter(field="status'; DROP TABLE orders; --", operator="eq", value="val")

    with pytest.raises(ValidationError):
        TimeRange(field="date OR 1=1", start="2026-01-01")


def test_filter_operators_validation():
    # in operator requires non-empty sequence
    with pytest.raises(ValidationError):
        PlanFilter(field="status", operator="in", value="not_a_list")

    with pytest.raises(ValidationError):
        PlanFilter(field="status", operator="in", value=[])

    # between requires 2 elements
    with pytest.raises(ValidationError):
        PlanFilter(field="amount", operator="between", value=[10])

    with pytest.raises(ValidationError):
        PlanFilter(field="amount", operator="between", value=[10, 20, 30])

    valid_between = PlanFilter(field="amount", operator="between", value=[10, 50])
    assert valid_between.value == [10, 50]

    # is_null ignores value
    filter_null = PlanFilter(field="notes", operator="is_null", value="something")
    assert filter_null.value is None


def test_filter_arbitrary_object_rejected():
    class ArbitraryObj:
        pass

    with pytest.raises(ValidationError):
        PlanFilter(field="status", operator="eq", value=ArbitraryObj())
