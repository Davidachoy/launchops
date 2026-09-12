import os
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def get_database_url() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://launchops:launchops@localhost:5432/launchops",
    )


engine: Engine = create_engine(
    get_database_url(),
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """Yield a DB session; roll back on error and always close afterward."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection() -> int:
    """Open a session, run SELECT 1, return 1, then close the session."""
    db = SessionLocal()
    try:
        value = db.execute(text("SELECT 1")).scalar_one()
        return int(value)
    finally:
        db.close()
