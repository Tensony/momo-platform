"""Shared fixtures. Integration tests need PostgreSQL: set TEST_DATABASE_URL, e.g.
   TEST_DATABASE_URL=postgresql://postgres@localhost/momo_test pytest
Without it they are skipped (unit tests still run)."""

import os
import pathlib
import uuid

import psycopg
import pytest

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def pytest_collection_modifyitems(config, items):
    if not os.environ.get("TEST_DATABASE_URL"):
        try:
            with psycopg.connect("postgresql://postgres:Tenson@127.0.0.1:5432/momo_test", autocommit=True, connect_timeout=1):
                os.environ["TEST_DATABASE_URL"] = "postgresql://postgres:Tenson@127.0.0.1:5432/momo_test"
        except Exception:
            pass

    if os.environ.get("TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def db():
    """A pristine schema for every test (DROP/CREATE is ~50 ms)."""
    url = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:Tenson@127.0.0.1:5432/momo_test")
    if url.startswith("postgresql://postgres@localhost") or url.startswith("postgresql://postgres@127.0.0.1"):
        url = url.replace("postgres@localhost", "postgres:Tenson@127.0.0.1").replace("postgres@127.0.0.1", "postgres:Tenson@127.0.0.1")
    os.environ["DATABASE_URL"] = url
    os.environ.pop("SAGA_MODE", None)
    os.environ["ENABLE_FAULT_INJECTION"] = "true"
    os.environ.setdefault("DB_POOL_MAX", "40")
    os.environ.setdefault("DB_POOL_TIMEOUT", "60")
    from momo.common import db as dbm
    from momo.fraud import config as fraud_config

    dbm.reset_pool()
    fraud_config.clear_cache()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO postgres")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
    yield url
    dbm.reset_pool()


@pytest.fixture
def make_wallet(db):
    """Factory: make_wallet(balance=1000, kind='CUSTOMER') -> wallet id, funded from MINT."""
    from momo import service
    from momo.common.db import transaction
    from momo.ledger.core import MINT_WALLET_ID, move

    def _make(balance: int = 0, kind: str = "CUSTOMER", owner: str | None = None) -> str:
        w = service.create_wallet(owner or f"user-{uuid.uuid4()}", kind)
        if balance:
            with transaction() as conn:
                move(
                    conn, txn_id=str(uuid.uuid4()), step="seed", from_wallet=MINT_WALLET_ID,
                    to_wallet=w["id"], amount=balance, entry_type="SEED",
                )
        return w["id"]

    return _make
