from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.mappers import row_to_event
from app.db.models import Environment as EnvironmentRow
from app.db.models import EnvironmentEvent as EnvironmentEventRow
from app.models import EnvironmentStatus

VALID_TRANSITIONS: dict[EnvironmentStatus, set[EnvironmentStatus]] = {
    EnvironmentStatus.REQUESTED: {
        EnvironmentStatus.QUEUED,
        EnvironmentStatus.DELETING,
    },
    EnvironmentStatus.QUEUED: {
        EnvironmentStatus.PROVISIONING,
        EnvironmentStatus.DELETING,
    },
    EnvironmentStatus.PROVISIONING: {
        EnvironmentStatus.READY,
        EnvironmentStatus.FAILED,
        EnvironmentStatus.DELETING,
    },
    EnvironmentStatus.READY: {
        EnvironmentStatus.EXPIRED,
        EnvironmentStatus.DELETING,
    },
    EnvironmentStatus.EXPIRED: {EnvironmentStatus.DELETING},
    EnvironmentStatus.DELETING: {EnvironmentStatus.DELETED},
}


def transition_environment(
    db: Session,
    row: EnvironmentRow,
    new_status: EnvironmentStatus,
    actor: str,
    reason: str,
    correlation_id: str,
) -> EnvironmentEvent:
    current_status = EnvironmentStatus(row.status)
    allowed = VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise ValueError(
            f"invalid transition: {current_status.value} -> {new_status.value}"
        )

    timestamp = datetime.now(timezone.utc)
    event_row = EnvironmentEventRow(
        environment_id=row.id,
        from_status=current_status.value,
        to_status=new_status.value,
        timestamp=timestamp,
        actor=actor,
        reason=reason,
        correlation_id=correlation_id,
    )
    row.status = new_status.value
    db.add(event_row)
    db.flush()
    return row_to_event(event_row)
