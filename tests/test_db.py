import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import check_connection


def test_postgres_select_one() -> None:
    try:
        check_connection()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
