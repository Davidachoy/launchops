import os

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


def check_connection() -> None:
    """Open a connection and run SELECT 1. Raises if PostgreSQL is unreachable."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
