"""Database access.

Connection modes
----------------
* Local / CI:  DATABASE_URL=postgresql://... (a local throw-away database only).
* AWS:         IAM database authentication. There is NO static database password for the app tier:
               every new connection signs a short-lived (15 min) token with the task/Lambda role.
               The only DB password lives in Secrets Manager (RDS-managed master secret) and is read
               by the one-off db-init Lambda that applies the schema and creates the `momo_app` role.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}


def _iam_token(host: str, port: int, user: str, region: str) -> str:
    key = f"{host}:{port}:{user}"
    cached = _token_cache.get(key)
    if cached and cached[1] > time.time():
        return cached[0]
    import boto3

    token = boto3.client("rds", region_name=region).generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=user, Region=region
    )
    _token_cache[key] = (token, time.time() + 600)  # tokens live 15 min; refresh after 10
    return token


class IamConnection(psycopg.Connection):
    """Connection class that injects a fresh IAM auth token as the password on every connect."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
        host = os.environ["DB_HOST"]
        port = int(os.environ.get("DB_PORT", "5432"))
        user = os.environ.get("DB_USER", "momo_app")
        region = os.environ.get("AWS_REGION") or os.environ["AWS_DEFAULT_REGION"]
        kwargs["password"] = _iam_token(host, port, user, region)
        return super().connect(conninfo, **kwargs)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        max_size = int(os.environ.get("DB_POOL_MAX", "8"))
        min_size = int(os.environ.get("DB_POOL_MIN", "1"))
        url = os.environ.get("DATABASE_URL")
        common = dict(
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            check=ConnectionPool.check_connection,
            timeout=float(os.environ.get("DB_POOL_TIMEOUT", "30")),
            open=True,
        )
        if url:
            _pool = ConnectionPool(url, **common)
        else:
            host = os.environ["DB_HOST"]
            port = os.environ.get("DB_PORT", "5432")
            name = os.environ.get("DB_NAME", "momo")
            user = os.environ.get("DB_USER", "momo_app")
            conninfo = f"host={host} port={port} dbname={name} user={user} sslmode=require"
            _pool = ConnectionPool(conninfo, connection_class=IamConnection, **common)
        return _pool


def reset_pool() -> None:
    """Close and forget the pool (used by tests)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """One database transaction: commit on success, roll back on any exception."""
    with get_pool().connection() as conn:
        yield conn


def master_dsn_from_secret(secret_arn: str, host: str, dbname: str) -> str:
    """Used only by the db-init Lambda: master credentials come from Secrets Manager."""
    import boto3

    secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
    )
    return (
        f"host={host} port={secret.get('port', 5432)} dbname={dbname} "
        f"user={secret['username']} password={secret['password']} sslmode=require"
    )
