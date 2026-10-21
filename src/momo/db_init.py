"""db-init Lambda: applies db/schema.sql and prepares the least-privilege application role.

Runs once from Terraform (aws_lambda_invocation) after the Aurora cluster is up, so a clean AWS
account needs no manual SQL. It is the only code that ever sees the master password, which it reads
from Secrets Manager (RDS-managed secret) - never from environment variables or source control.
Idempotent: safe to run on every `terraform apply`.
"""

from __future__ import annotations

import logging
import os
import pathlib

import psycopg

from .common.db import master_dsn_from_secret
from .common.logging_setup import configure_logging

configure_logging("db-init")
log = logging.getLogger("db-init")

APP_ROLE_SQL = """
ALTER ROLE momo_app LOGIN;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    GRANT rds_iam TO momo_app;            -- IAM database authentication: no password at all
  END IF;
END $$;
GRANT CONNECT ON DATABASE {dbname} TO momo_app;
GRANT USAGE ON SCHEMA public TO momo_app;
"""


def handler(event=None, context=None):
    host, dbname = os.environ["DB_HOST"], os.environ.get("DB_NAME", "momo")
    schema = pathlib.Path(os.environ.get("SCHEMA_PATH", "/var/task/schema.sql")).read_text()
    dsn = master_dsn_from_secret(os.environ["DB_MASTER_SECRET_ARN"], host, dbname)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(schema)
        conn.execute(APP_ROLE_SQL.format(dbname=dbname))
        tables = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchone()[0]
    log.info("schema applied", extra={"tables": tables})
    return {"status": "ok", "tables": tables}
