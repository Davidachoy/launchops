"""SQLAlchemy persistence models.

Separate from app.models (Pydantic API/domain contracts):
- app.models = request/response and domain shapes
- app.db.models = how rows are stored in PostgreSQL
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Environment(Base):
    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    runtime: Mapped[str] = mapped_column(String(255), nullable=False)
    cpu: Mapped[str] = mapped_column(String(64), nullable=False)
    memory: Mapped[str] = mapped_column(String(64), nullable=False)
    slo_availability: Mapped[float] = mapped_column(Float, nullable=False)
    ttl_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    events: Mapped[list["EnvironmentEvent"]] = relationship(
        back_populates="environment",
    )
    # One create request key maps to one environment (and vice versa for that create).
    idempotency_record: Mapped["IdempotencyRecord | None"] = relationship(
        back_populates="environment",
        uselist=False,
    )


class EnvironmentEvent(Base):
    __tablename__ = "environment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("environments.id"),
        nullable=False,
        index=True,
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    environment: Mapped[Environment] = relationship(back_populates="events")


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    environment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("environments.id"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    environment: Mapped[Environment] = relationship(
        back_populates="idempotency_record"
    )
