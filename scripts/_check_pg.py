"""Quick PostgreSQL setup verification for momo_test."""
import os
import sys

import psycopg

PG_URL = os.environ.get(
    "PG_ADMIN_URL", "postgresql://postgres@127.0.0.1:5432/postgres"
)
TEST_DB = "momo_test"
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/momo_test"
)

try:
    conn = psycopg.connect(PG_URL, connect_timeout=3, autocommit=True)
    print(f"[OK] Connected to PostgreSQL server version {conn.info.server_version}")

    cur = conn.execute("SELECT datname FROM pg_database WHERE datname = %s", (TEST_DB,))
    row = cur.fetchone()
    if row:
        print(f"[OK] Database '{TEST_DB}' already exists")
    else:
        print(f"[..] Database '{TEST_DB}' not found — creating it now...")
        conn.execute(f'CREATE DATABASE {TEST_DB}')
        print(f"[OK] Database '{TEST_DB}' created successfully")
    conn.close()

    conn2 = psycopg.connect(TEST_DB_URL, connect_timeout=3)
    print(f"[OK] Connected to '{TEST_DB}' — ready for integration tests!")
    conn2.close()

except psycopg.OperationalError as e:
    print(f"[FAIL] Connection error: {e}")
    sys.exit(1)
