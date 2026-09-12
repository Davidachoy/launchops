"""Map between Pydantic API models and SQLAlchemy persistence rows."""

from datetime import datetime

from app.db.models import Environment as EnvironmentRow
from app.db.models import EnvironmentEvent as EnvironmentEventRow
from app.models import (
    DesiredState,
    Environment,
    EnvironmentCreate,
    EnvironmentEvent,
    EnvironmentStatus,
    ResourceSpec,
    SLOSpec,
)


def environment_create_to_row(
    payload: EnvironmentCreate,
    *,
    environment_id: str,
    created_at: datetime,
    expires_at: datetime,
) -> EnvironmentRow:
    return EnvironmentRow(
        id=environment_id,
        owner=payload.owner,
        repo=payload.repo,
        runtime=payload.runtime,
        cpu=payload.resources.cpu,
        memory=payload.resources.memory,
        slo_availability=payload.slo.availability,
        ttl_hours=payload.ttl_hours,
        desired_state=DesiredState.ACTIVE.value,
        status=EnvironmentStatus.REQUESTED.value,
        created_at=created_at,
        expires_at=expires_at,
        last_error=None,
    )


def row_to_environment(row: EnvironmentRow) -> Environment:
    return Environment(
        id=row.id,
        owner=row.owner,
        repo=row.repo,
        runtime=row.runtime,
        resources=ResourceSpec(cpu=row.cpu, memory=row.memory),
        slo=SLOSpec(availability=row.slo_availability),
        ttl_hours=row.ttl_hours,
        desired_state=DesiredState(row.desired_state),
        status=EnvironmentStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_error=row.last_error,
    )


def row_to_event(row: EnvironmentEventRow) -> EnvironmentEvent:
    return EnvironmentEvent(
        environment_id=row.environment_id,
        from_status=EnvironmentStatus(row.from_status),
        to_status=EnvironmentStatus(row.to_status),
        timestamp=row.timestamp,
        actor=row.actor,
        reason=row.reason,
        correlation_id=row.correlation_id,
    )
