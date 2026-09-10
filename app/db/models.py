"""SQLAlchemy persistence models.

These are separate from app.models (Pydantic API/domain contracts).
Tables will be added in a later step — this module only holds the declarative Base.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
