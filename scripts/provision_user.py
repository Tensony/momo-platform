#!/usr/bin/env python3
"""Provision an agent or administrator through a trusted operator environment."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from momo.api.auth import hash_password
from momo.api.user_store import normalize_phone, validate_password_strength
from momo.common.db import transaction
from momo.common.errors import InvalidRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("agent", "admin"))
    parser.add_argument("--identifier", required=True, help="Agent ID or administrator username/email")
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--phone", help="Optional registered phone number")
    parser.add_argument("--email", help="Optional email address")
    args = parser.parse_args()

    if not (os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST")):
        parser.error("Set DATABASE_URL or configure DB_HOST and AWS_REGION before provisioning.")
    if not args.identifier.strip() or not args.full_name.strip():
        parser.error("Identifier and full name must not be blank.")

    try:
        phone = normalize_phone(args.phone) if args.phone else None
    except InvalidRequest as exc:
        parser.error(str(exc))
    email = args.email.strip().lower() if args.email else None
    password = getpass.getpass("Initial password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match.")
    try:
        validate_password_strength(password)
    except InvalidRequest as exc:
        parser.error(str(exc))

    identifier = args.identifier.strip()
    username = identifier.lower() if args.role == "admin" else identifier
    user_id = str(uuid.uuid4())
    try:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO users (id, username, phone, email, role, password_hash, full_name, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE')""",
                (
                    user_id,
                    username,
                    phone,
                    email,
                    args.role,
                    hash_password(password),
                    args.full_name.strip(),
                ),
            )
    except psycopg.errors.UniqueViolation:
        print("An account already uses this identifier, phone, or email.", file=sys.stderr)
        return 1

    print(f"Provisioned {args.role} user {username} ({user_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
