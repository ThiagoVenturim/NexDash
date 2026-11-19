"""Synthetic example cubes (sales, support, stock) and deterministic demo data.

The data is generated from a fixed seed, so every run produces the same rows.
Nothing here is real: names, values and dates are invented for demonstrations
and offline tests.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable, DropTable

from nexdash.cube import CubeRegistry
from nexdash.executor import DatabaseExecutor, SQLiteExecutor

DEMO_CUBES_DIR = Path(__file__).parent / "cubes"
DEMO_CUBES = ("sales", "stock", "support")
DEFAULT_SEED = 7

_START = date(2026, 1, 1)
_DAYS = 181  # January to June 2026

_REGIONS = ("north", "south", "east", "west")
_CHANNELS = ("online", "store", "partner")
_PRODUCTS = {
    "Laptop 14": 1200.0,
    "Monitor 27": 320.0,
    "Keyboard": 45.0,
    "Mouse": 25.0,
    "Headset": 80.0,
    "Dock": 150.0,
}

_PRIORITIES = ("low", "medium", "high", "urgent")
_CATEGORIES = ("billing", "bug", "access", "question")
_SUPPORT_CHANNELS = ("email", "chat", "phone")

_WAREHOUSES = ("central", "coastal", "inland")
_STOCK_ITEMS = (
    ("HW-001", "Laptop 14", "hardware", 900.0),
    ("HW-002", "Monitor 27", "hardware", 210.0),
    ("HW-003", "Dock", "hardware", 95.0),
    ("AC-001", "Keyboard", "accessories", 22.0),
    ("AC-002", "Mouse", "accessories", 11.0),
    ("AC-003", "Headset", "accessories", 38.0),
    ("CO-001", "Printer paper", "consumables", 4.5),
    ("CO-002", "Toner", "consumables", 60.0),
)

_metadata = sa.MetaData()

SALES_TABLE = sa.Table(
    "demo_sales",
    _metadata,
    sa.Column("sale_id", sa.String(16), primary_key=True),
    sa.Column("sale_date", sa.String(10), nullable=False),
    sa.Column("month", sa.String(7), nullable=False),
    sa.Column("region", sa.String(16), nullable=False),
    sa.Column("channel", sa.String(16), nullable=False),
    sa.Column("product", sa.String(64), nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("amount", sa.Float, nullable=False),
)

TICKETS_TABLE = sa.Table(
    "demo_tickets",
    _metadata,
    sa.Column("ticket_id", sa.String(16), primary_key=True),
    sa.Column("opened_date", sa.String(10), nullable=False),
    sa.Column("month", sa.String(7), nullable=False),
    sa.Column("priority", sa.String(16), nullable=False),
    sa.Column("category", sa.String(16), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("channel", sa.String(16), nullable=False),
    sa.Column("resolution_hours", sa.Float, nullable=True),
    sa.Column("csat", sa.Integer, nullable=True),
)

STOCK_TABLE = sa.Table(
    "demo_stock",
    _metadata,
    sa.Column("sku", sa.String(16), primary_key=True),
    sa.Column("warehouse", sa.String(16), primary_key=True),
    sa.Column("product", sa.String(64), nullable=False),
    sa.Column("category", sa.String(16), nullable=False),
    sa.Column("on_hand", sa.Integer, nullable=False),
    sa.Column("reorder_point", sa.Integer, nullable=False),
    sa.Column("below_reorder", sa.String(3), nullable=False),
    sa.Column("unit_cost", sa.Float, nullable=False),
    sa.Column("stock_value", sa.Float, nullable=False),
)


def load_demo_registry() -> CubeRegistry:
    """Return a registry with the synthetic sales, support and stock cubes."""
    return CubeRegistry.from_directory(DEMO_CUBES_DIR)


def generate_sales(rng: random.Random, count: int = 240) -> list[dict[str, Any]]:
    rows = []
    products = list(_PRODUCTS.items())
    for index in range(count):
        day = _START + timedelta(days=rng.randrange(_DAYS))
        product, price = rng.choice(products)
        quantity = rng.randint(1, 5)
        discount = rng.choice((1.0, 1.0, 0.95, 0.9))
        rows.append(
            {
                "sale_id": f"S{index + 1:05d}",
                "sale_date": day.isoformat(),
                "month": day.strftime("%Y-%m"),
                "region": rng.choice(_REGIONS),
                "channel": rng.choice(_CHANNELS),
                "product": product,
                "quantity": quantity,
                "amount": round(price * quantity * discount, 2),
            }
        )
    return rows


def generate_tickets(rng: random.Random, count: int = 180) -> list[dict[str, Any]]:
    rows = []
    base_hours = {"urgent": 4.0, "high": 12.0, "medium": 30.0, "low": 60.0}
    for index in range(count):
        day = _START + timedelta(days=rng.randrange(_DAYS))
        priority = rng.choice(_PRIORITIES)
        status = rng.choices(("resolved", "open", "pending"), weights=(7, 2, 1))[0]
        resolved = status == "resolved"
        rows.append(
            {
                "ticket_id": f"T{index + 1:05d}",
                "opened_date": day.isoformat(),
                "month": day.strftime("%Y-%m"),
                "priority": priority,
                "category": rng.choice(_CATEGORIES),
                "status": status,
                "channel": rng.choice(_SUPPORT_CHANNELS),
                "resolution_hours": round(base_hours[priority] * rng.uniform(0.3, 1.8), 1) if resolved else None,
                "csat": rng.choices((1, 2, 3, 4, 5), weights=(1, 1, 2, 4, 4))[0] if resolved else None,
            }
        )
    return rows


def generate_stock(rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    for sku, product, category, unit_cost in _STOCK_ITEMS:
        for warehouse in _WAREHOUSES:
            on_hand = rng.randint(0, 120)
            reorder_point = rng.randint(10, 40)
            rows.append(
                {
                    "sku": sku,
                    "warehouse": warehouse,
                    "product": product,
                    "category": category,
                    "on_hand": on_hand,
                    "reorder_point": reorder_point,
                    "below_reorder": "yes" if on_hand < reorder_point else "no",
                    "unit_cost": unit_cost,
                    "stock_value": round(on_hand * unit_cost, 2),
                }
            )
    return rows


def generate_demo_data(seed: int = DEFAULT_SEED) -> dict[str, list[dict[str, Any]]]:
    """Return synthetic rows keyed by table name. The same seed yields the same rows."""
    rng = random.Random(seed)  # noqa: S311 - synthetic demo data, not security sensitive
    return {
        SALES_TABLE.name: generate_sales(rng),
        TICKETS_TABLE.name: generate_tickets(rng),
        STOCK_TABLE.name: generate_stock(rng),
    }


def seed_demo_data(executor: DatabaseExecutor | SQLiteExecutor, seed: int = DEFAULT_SEED) -> dict[str, int]:
    """Create the demo tables (dropping earlier copies) and insert synthetic rows.

    Returns the number of rows inserted per table.
    """
    data = generate_demo_data(seed)
    tables = {table.name: table for table in _metadata.sorted_tables}

    if isinstance(executor, SQLiteExecutor):
        _seed_sqlite_connection(executor, data, tables)
    else:
        with executor.engine.begin() as conn:
            _metadata.drop_all(conn)
            _metadata.create_all(conn)
            for name, rows in data.items():
                conn.execute(tables[name].insert(), rows)

    return {name: len(rows) for name, rows in data.items()}


def _seed_sqlite_connection(
    executor: SQLiteExecutor,
    data: dict[str, list[dict[str, Any]]],
    tables: dict[str, sa.Table],
) -> None:
    dialect = sqlite.dialect()
    conn = executor.connection
    for name, table in tables.items():
        conn.execute(str(DropTable(table, if_exists=True).compile(dialect=dialect)))
        conn.execute(str(CreateTable(table).compile(dialect=dialect)))
        insert_sql = str(table.insert().compile(dialect=sqlite.dialect(paramstyle="named")))
        conn.executemany(insert_sql, data[name])
    conn.commit()


__all__ = [
    "DEFAULT_SEED",
    "DEMO_CUBES",
    "DEMO_CUBES_DIR",
    "SALES_TABLE",
    "STOCK_TABLE",
    "TICKETS_TABLE",
    "generate_demo_data",
    "load_demo_registry",
    "seed_demo_data",
]
