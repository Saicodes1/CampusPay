import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from campuspay.api import create_app
from campuspay.config import Settings
from campuspay.db import migrate
from campuspay.payments import Payments


@pytest.fixture(scope="session")
def settings():
    return Settings(
        _env_file=None,
        database_url=os.getenv(
            "CAMPUSPAY_TEST_DATABASE_URL",
            "postgresql://campuspay:campuspay-local-only@127.0.0.1:55433/campuspay_test",
        ),
        redis_url=os.getenv("CAMPUSPAY_TEST_REDIS_URL", "redis://127.0.0.1:56380/15"),
        rabbit_url=os.getenv(
            "CAMPUSPAY_TEST_RABBIT_URL", "amqp://campuspay:campuspay-local-only@127.0.0.1:5674/%2F"
        ),
        demo_password="campuspay-demo-only",
        rate_limit=10000,
        auth_rate_limit=10000,
    )


@pytest.fixture(scope="session")
def database(settings):
    # A unique schema + dedicated database: never truncate the demo application schema.
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(settings.database_url)["dbname"] != "campuspay_test":
        pytest.fail("Integration tests require a database named campuspay_test")
    schema = "test_" + uuid4().hex
    with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db = ConnectionPool(
        settings.database_url,
        min_size=2,
        max_size=24,
        timeout=10,
        open=True,
        kwargs={
            "row_factory": dict_row,
            "connect_timeout": 5,
            "options": f"-c search_path={schema} -c lock_timeout=5000 -c statement_timeout=15000",
        },
    )
    db.wait()
    migrate(db)
    yield db
    db.close()
    with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db(database):
    with database.connection() as conn:
        conn.execute(
            "TRUNCATE accounts,journals,entries,intents,idempotency,outbox,inbox,webhook_deliveries RESTART IDENTITY CASCADE"
        )
        conn.execute((Path(__file__).parents[1] / "campuspay/migrations/002_demo_accounts.sql").read_text())
    return database


@pytest.fixture
def payments(db):
    return Payments(db)


@pytest.fixture
def cache(settings):
    client = redis.Redis.from_url(settings.redis_url, socket_timeout=1)
    assert client.connection_pool.connection_kwargs["db"] == 15, "Tests must use isolated Redis DB 15"
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture
def client(settings, db, cache):
    with TestClient(create_app(settings, db, cache), raise_server_exceptions=False) as client:
        yield client


def balance(db, account="alice"):
    with db.connection() as conn:
        return conn.execute(
            "SELECT balance,reserved,balance-reserved AS available FROM accounts WHERE id=%s", (account,)
        ).fetchone()


def scalar(db, query, args=()):
    with db.connection() as conn:
        return next(iter(conn.execute(query, args).fetchone().values()))


def captured(payments, amount=1000):
    from uuid import UUID

    payments.fund("alice", "seed", amount)
    intent = UUID(payments.create_intent("cafe", "intent", amount)["id"])
    payments.authorize("alice", "auth", intent)
    payments.capture("cafe", "capture", intent)
    return intent
