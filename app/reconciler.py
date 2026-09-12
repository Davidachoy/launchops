from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Environment as EnvironmentRow
from app.models import DesiredState, EnvironmentEvent, EnvironmentStatus
from app.state import transition_environment


def _health_check_passed(_row: EnvironmentRow) -> bool:
    # Simulated probe — always healthy in this Lab 1 stub.
    return True


def expire_if_needed(
    db: Session,
    row: EnvironmentRow,
    now: datetime,
    correlation_id: str,
) -> EnvironmentEvent | None:
    """Apply TTL against the persisted row. Does not commit."""
    if row.desired_state != DesiredState.ACTIVE.value:
        return None
    if row.expires_at > now:
        return None

    status = EnvironmentStatus(row.status)

    if status == EnvironmentStatus.READY:
        event = transition_environment(
            db,
            row,
            EnvironmentStatus.EXPIRED,
            actor="reconciler",
            reason="ttl expired",
            correlation_id=correlation_id,
        )
        row.desired_state = DesiredState.DELETED.value
        return event

    if status in {
        EnvironmentStatus.REQUESTED,
        EnvironmentStatus.QUEUED,
        EnvironmentStatus.PROVISIONING,
    }:
        event = transition_environment(
            db,
            row,
            EnvironmentStatus.DELETING,
            actor="reconciler",
            reason="ttl expired during pending operation",
            correlation_id=correlation_id,
        )
        row.desired_state = DesiredState.DELETED.value
        return event

    return None


def _reconcile_active(
    db: Session,
    row: EnvironmentRow,
    correlation_id: str,
) -> EnvironmentEvent | None:
    status = EnvironmentStatus(row.status)

    if status == EnvironmentStatus.REQUESTED:
        return transition_environment(
            db,
            row,
            EnvironmentStatus.QUEUED,
            actor="reconciler",
            reason="accepted for provisioning",
            correlation_id=correlation_id,
        )

    if status == EnvironmentStatus.QUEUED:
        return transition_environment(
            db,
            row,
            EnvironmentStatus.PROVISIONING,
            actor="reconciler",
            reason="starting provisioning",
            correlation_id=correlation_id,
        )

    if status == EnvironmentStatus.PROVISIONING:
        if not _health_check_passed(row):
            return None
        return transition_environment(
            db,
            row,
            EnvironmentStatus.READY,
            actor="reconciler",
            reason="health check passed",
            correlation_id=correlation_id,
        )

    return None


def _reconcile_deleted(
    db: Session,
    row: EnvironmentRow,
    correlation_id: str,
) -> EnvironmentEvent | None:
    status = EnvironmentStatus(row.status)

    if status == EnvironmentStatus.DELETED:
        return None

    if status == EnvironmentStatus.DELETING:
        return transition_environment(
            db,
            row,
            EnvironmentStatus.DELETED,
            actor="reconciler",
            reason="cleanup completed",
            correlation_id=correlation_id,
        )

    return transition_environment(
        db,
        row,
        EnvironmentStatus.DELETING,
        actor="reconciler",
        reason="manual delete requested",
        correlation_id=correlation_id,
    )


def reconcile_environment(
    db: Session,
    environment_id: str,
    correlation_id: str,
    now: datetime | None = None,
) -> EnvironmentEvent | None:
    """Load a persisted environment and apply at most one status transition.

    status update + EnvironmentEvent INSERT happen in one transaction:
    commit exactly once at the end, or roll back if anything fails.
    """
    current_time = now if now is not None else datetime.now(timezone.utc)

    try:
        row = db.get(EnvironmentRow, environment_id)
        if row is None:
            raise ValueError(f"environment not found: {environment_id}")

        event = expire_if_needed(db, row, current_time, correlation_id)
        if event is None:
            if row.desired_state == DesiredState.ACTIVE.value:
                event = _reconcile_active(db, row, correlation_id)
            elif row.desired_state == DesiredState.DELETED.value:
                event = _reconcile_deleted(db, row, correlation_id)

        db.commit()
        return event
    except Exception:
        db.rollback()
        raise
