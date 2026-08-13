"""Create the first named administrator through an offline operator command."""

from __future__ import annotations

import argparse
import secrets
import sys
from uuid import uuid4

from pydantic import ValidationError
from smart_commissioning_core.db.repositories import (
    ActiveAdminExistsError,
    UserRepository,
)
from smart_commissioning_core.rbac import Role
from sqlalchemy.exc import IntegrityError

from app.core.auth import hash_api_key
from app.core.config import get_settings
from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories
from app.schemas.users import CreateUserRequest

_KEY_ENTROPY_BYTES = 32


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.bootstrap_admin")
    parser.add_argument("--username", required=True)
    return parser


def _validated_username(raw_username: str) -> str:
    try:
        request = CreateUserRequest(
            username=raw_username.strip(),
            role=Role.ADMIN,
        )
    except ValidationError as error:
        raise ValueError("username must contain between 1 and 255 characters") from error
    return request.username


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = get_settings()
        if settings.deployment_role not in {"edge", "hub"}:
            print(
                "ERROR: named-admin bootstrap is available only on edge or hub deployments.",
                file=sys.stderr,
            )
            return 2
        try:
            username = _validated_username(args.username)
        except ValueError:
            print(
                "ERROR: username must contain between 1 and 255 characters.",
                file=sys.stderr,
            )
            return 2

        ensure_runtime_directories()
        raw_key = secrets.token_urlsafe(_KEY_ENTROPY_BYTES)
        UserRepository(get_engine()).create_bootstrap_admin(
            user_id=str(uuid4()),
            username=username,
            api_key_hash=hash_api_key(raw_key),
        )
    except ActiveAdminExistsError:
        print(
            "ERROR: an active named administrator already exists; bootstrap refused.",
            file=sys.stderr,
        )
        return 2
    except IntegrityError:
        print(
            "ERROR: administrator bootstrap conflicted with existing or concurrent data.",
            file=sys.stderr,
        )
        return 2
    except Exception:  # noqa: BLE001 - operator CLI must fail without secret-bearing traceback
        print("ERROR: administrator bootstrap failed.", file=sys.stderr)
        return 2

    print("Created the named administrator with a one-time API key.")
    print("Copy this key now; the database stores only its SHA-256 hash:")
    print(raw_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
