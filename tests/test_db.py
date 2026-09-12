from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal, check_connection, get_db


def test_postgres_select_one_via_session() -> None:
    try:
        assert check_connection() == 1
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")


def test_get_db_yields_session_and_closes() -> None:
    try:
        generator: Iterator = get_db()
        db = next(generator)
        assert db.execute(text("SELECT 1")).scalar_one() == 1
        # Closing the generator runs get_db()'s finally and closes the session.
        generator.close()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")


def test_session_local_select_one_lifecycle() -> None:
    try:
        db = SessionLocal()
        try:
            assert db.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            db.close()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
