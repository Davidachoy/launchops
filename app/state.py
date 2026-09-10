from datetime import datetime, timezone

from app.models import Environment, EnvironmentEvent, EnvironmentStatus

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

events_by_environment: dict[str, list[EnvironmentEvent]] = {}


def transition_environment(
    environment: Environment,
    new_status: EnvironmentStatus,
    actor: str,
    reason: str,
    correlation_id: str,
) -> EnvironmentEvent:
    current_status = environment.status
    allowed = VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise ValueError(
            f"invalid transition: {current_status.value} -> {new_status.value}"
        )

    event = EnvironmentEvent(
        environment_id=environment.id,
        from_status=current_status,
        to_status=new_status,
        timestamp=datetime.now(timezone.utc),
        actor=actor,
        reason=reason,
        correlation_id=correlation_id,
    )
    environment.status = new_status
    events_by_environment.setdefault(environment.id, []).append(event)
    return event
