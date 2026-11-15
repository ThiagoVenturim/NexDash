"""Tests for pluggable database layer, SQLAlchemy executor, and multi-dialect support."""

import os
import sqlite3

import pytest
import sqlalchemy as sa

from nexdash.compiler import SQLCompiler
from nexdash.cube import Cube, Dimension, FilterRule, Join, Metric
from nexdash.db import (
    check_database_connection,
    create_database_engine,
    get_db_url,
    init_db_schema,
    load_env_file,
    sanitize_db_url,
)
from nexdash.executor import DatabaseExecutor, SQLAlchemyExecutor, get_executor
from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan
from nexdash.reports import ReportSpec, ReportStore
from nexdash.server import NexDashRequestHandler
from nexdash.sources import collect_single_source


@pytest.fixture
def sample_cube() -> Cube:
    return Cube(
        name="sales",
        source="fact_sales",
        dimensions=[
            Dimension(name="region", type="string"),
            Dimension(name="product_id", type="string"),
        ],
        metrics=[
            Metric(name="total_revenue", aggregation="sum", column="amount"),
            Metric(name="sales_count", aggregation="count"),
        ],
        joins=[
            Join(
                name="products",
                target_table="dim_products",
                source_key="product_id",
                target_key="id",
            )
        ],
        filters=[
            FilterRule(name="region"),
        ],
    )


@pytest.fixture
def seeded_db_executor() -> DatabaseExecutor:
    engine = create_database_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                CREATE TABLE fact_sales (
                    id TEXT PRIMARY KEY,
                    region TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    amount REAL NOT NULL
                )
                """
            )
        )
        conn.execute(
            sa.text(
                """
                CREATE TABLE dim_products (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO fact_sales (id, region, product_id, amount) VALUES
                ('s1', 'North', 'p1', 100.0),
                ('s2', 'North', 'p2', 150.0),
                ('s3', 'South', 'p1', 200.0),
                ('s4', 'South', 'p2', 300.0)
                """
            )
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO dim_products (id, name, category) VALUES
                ('p1', 'Widget A', 'Hardware'),
                ('p2', 'Widget B', 'Hardware')
                """
            )
        )

    executor = DatabaseExecutor(engine)
    yield executor
    executor.close()


def test_sanitize_db_url():
    assert sanitize_db_url("sqlite:///:memory:") == "sqlite:///:memory:"
    assert (
        sanitize_db_url("postgresql+psycopg://user:secretpass@localhost:5432/testdb")
        == "postgresql+psycopg://user:****@localhost:5432/testdb"
    )
    assert (
        sanitize_db_url("mysql+pymysql://root:mypass@127.0.0.1:3306/mydb")
        == "mysql+pymysql://root:****@127.0.0.1:3306/mydb"
    )


def test_get_db_url_env_resolution(monkeypatch):
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("NEXDASH_DB_URL", raising=False)
    assert get_db_url() == "sqlite:///:memory:"

    monkeypatch.setenv("DB_URL", "postgresql+psycopg://user:pass@host/db")
    assert get_db_url() == "postgresql+psycopg://user:pass@host/db"

    monkeypatch.delenv("DB_URL")
    monkeypatch.setenv("NEXDASH_DB_URL", "mysql+pymysql://user:pass@host/db")
    assert get_db_url() == "mysql+pymysql://user:pass@host/db"


def test_load_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_NEXDASH_VAR=hello_world\n# Comment line\nINVALID_LINE\n", encoding="utf-8")
    loaded = load_env_file(env_file)
    assert loaded.get("TEST_NEXDASH_VAR") == "hello_world"
    assert os.environ.get("TEST_NEXDASH_VAR") == "hello_world"


def test_database_engine_creation_and_health():
    engine = create_database_engine("sqlite:///:memory:")
    assert check_database_connection(engine) is True
    engine.dispose()


def test_get_executor_factory():
    exec_db = get_executor("sqlite:///:memory:")
    assert isinstance(exec_db, DatabaseExecutor)
    assert isinstance(exec_db, SQLAlchemyExecutor)
    exec_db.close()

    raw_conn = sqlite3.connect(":memory:")
    from nexdash.executor import SQLiteExecutor

    exec_sqlite = get_executor(raw_conn)
    assert isinstance(exec_sqlite, SQLiteExecutor)
    exec_sqlite.close()


def test_sqlalchemy_executor_execute_plan(seeded_db_executor: DatabaseExecutor, sample_cube: Cube):
    plan = QueryPlan(
        cube="sales",
        dimensions=["region"],
        metrics=["total_revenue", "sales_count"],
        filters=[PlanFilter(field="region", operator="eq", value="North")],
        order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    )

    result = seeded_db_executor.execute_plan(plan, sample_cube)
    assert len(result) == 1
    assert result[0]["region"] == "North"
    assert result[0]["total_revenue"] == 250.0
    assert result[0]["sales_count"] == 2


def test_sqlalchemy_executor_execute_sql(seeded_db_executor: DatabaseExecutor):
    result = seeded_db_executor.execute_sql(
        "SELECT region, amount FROM fact_sales WHERE region = :reg ORDER BY amount ASC",
        {"reg": "South"},
    )
    assert len(result) == 2
    assert result.columns == ["region", "amount"]
    assert result[0]["amount"] == 200.0
    assert result[1]["amount"] == 300.0


def test_sqlalchemy_executor_load_records():
    executor = DatabaseExecutor("sqlite:///:memory:")
    records = [
        {"item": "book", "qty": "10"},
        {"item": "pen", "qty": "25"},
    ]
    executor.load_records("inventory", records)
    res = executor.execute_sql("SELECT item, qty FROM inventory ORDER BY item ASC")
    assert len(res) == 2
    assert res[0]["item"] == "book"
    assert res[1]["item"] == "pen"
    executor.close()


def test_compiler_dialects(sample_cube: Cube):
    compiler = SQLCompiler()
    plan = QueryPlan(
        cube="sales",
        dimensions=["region"],
        metrics=["total_revenue"],
        limit=5,
    )

    for dialect in ("sqlite", "postgresql", "mysql", "mssql"):
        compiled = compiler.compile(plan, sample_cube, dialect=dialect)
        assert compiled.dialect == dialect
        assert "fact_sales" in compiled.sql
        assert compiled.statement is not None


def test_report_store_with_sqlalchemy_engine():
    engine = create_database_engine("sqlite:///:memory:")
    init_db_schema(engine)

    store = ReportStore(database=engine)
    code = store.generate_approval_code(created_by="tester")
    assert code.code is not None

    status = store.verify_approval_code(code.code)
    assert status.valid is True

    spec = ReportSpec(
        title="Revenue Overview",
        plan=QueryPlan(cube="sales", metrics=["total_revenue"]),
    )
    draft = store.create_draft("rev_report", spec=spec, approval_code=code.code, author="tester")
    assert draft.key == "rev_report"

    retrieved = store.get_report("rev_report")
    assert retrieved.key == "rev_report"
    assert retrieved.draft_spec is not None

    store.close()


def test_sql_source_with_database_executor(seeded_db_executor: DatabaseExecutor):
    source = {
        "name": "sales_query",
        "kind": "sql",
        "sql": "SELECT region, amount FROM fact_sales WHERE amount >= :min_amt ORDER BY amount DESC",
        "params": {"min_amt": 180},
    }
    rows = collect_single_source(source, sql_executor=seeded_db_executor)
    assert len(rows) == 2
    assert rows[0]["region"] == "South"
    assert rows[0]["amount"] == 300.0


def test_sql_source_with_connection_url(tmp_path):
    db_file = tmp_path / "custom.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE metrics (name TEXT, val INTEGER)")
    conn.execute("INSERT INTO metrics VALUES ('cpu', 42)")
    conn.commit()
    conn.close()

    source = {
        "name": "custom_db",
        "kind": "sql",
        "sql": "SELECT name, val FROM metrics",
        "connection": {"url": f"sqlite:///{db_file}"},
    }
    rows = collect_single_source(source)
    assert len(rows) == 1
    assert rows[0]["name"] == "cpu"
    assert rows[0]["val"] == 42


def test_server_request_handler_mock():
    from io import BytesIO

    engine = create_database_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE test_data (val TEXT)"))
        conn.execute(sa.text("INSERT INTO test_data VALUES ('ok')"))

    executor = DatabaseExecutor(engine)

    class MockServer:
        def __init__(self, exec_inst):
            self.executor = exec_inst

    class MockHandler(NexDashRequestHandler):
        def __init__(self, server, path, body=b"", method="GET"):
            self.server = server
            self.path = path
            self.command = method
            self.rfile = BytesIO(body)
            self.wfile = BytesIO()
            self.headers = {"Content-Length": str(len(body))}
            self.client_address = ("127.0.0.1", 12345)

        def send_response(self, code, message=None):
            self.status_code = code

        def send_header(self, keyword, value):
            pass

        def end_headers(self):
            pass

    # Health check
    handler = MockHandler(MockServer(executor), "/health", method="GET")
    handler.do_GET()
    assert handler.status_code == 200
    res_data = handler.wfile.getvalue().decode("utf-8")
    assert '"status": "ok"' in res_data
    assert '"dialect": "sqlite"' in res_data

    # Query endpoint
    query_body = b'{"sql": "SELECT val FROM test_data", "params": {}}'
    q_handler = MockHandler(MockServer(executor), "/api/query", body=query_body, method="POST")
    q_handler.do_POST()
    assert q_handler.status_code == 200
    q_res = q_handler.wfile.getvalue().decode("utf-8")
    assert '"rows": [{"val": "ok"}]' in q_res

    executor.close()
