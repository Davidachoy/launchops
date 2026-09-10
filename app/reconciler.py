from datetime import datetime, timezone

from app.models import DesiredState, Environment, EnvironmentEvent, EnvironmentStatus
from app.state import transition_environment


def _health_check_passed(environment: Environment) -> bool:
    # Simulated probe — always healthy in this Lab 1 stub.
    return True


def expire_if_needed(
    environment: Environment,
    now: datetime,
    correlation_id: str,
) -> EnvironmentEvent | None:
    if environment.desired_state != DesiredState.ACTIVE:
        return None
    if environment.expires_at > now:
        return None

    if environment.status == EnvironmentStatus.READY:
        event = transition_environment(
            environment,
            EnvironmentStatus.EXPIRED,
            actor="reconciler",
            reason="ttl expired",
            correlation_id=correlation_id,
        )
        environment.desired_state = DesiredState.DELETED
        return event

    if environment.status in {
        EnvironmentStatus.REQUESTED,
        EnvironmentStatus.QUEUED,
        EnvironmentStatus.PROVISIONING,
    }:
        event = transition_environment(
            environment,
            EnvironmentStatus.DELETING,
            actor="reconciler",
            reason="ttl expired during pending operation",
            correlation_id=correlation_id,
        )
        environment.desired_state = DesiredState.DELETED
        return event

    return None


def _reconcile_active(
    environment: Environment,
    correlation_id: str,
) -> EnvironmentEvent | None:
    if environment.status == EnvironmentStatus.REQUESTED:
        return transition_environment(
            environment,
            EnvironmentStatus.QUEUED,
            actor="reconciler",
            reason="accepted for provisioning",
            correlation_id=correlation_id,
        )

    if environment.status == EnvironmentStatus.QUEUED:
        return transition_environment(
            environment,
            EnvironmentStatus.PROVISIONING,
            actor="reconciler",
            reason="starting provisioning",
            correlation_id=correlation_id,
        )

    if environment.status == EnvironmentStatus.PROVISIONING:
        if not _health_check_passed(environment):
            return None
        return transition_environment(
            environment,
            EnvironmentStatus.READY,
            actor="reconciler",
            reason="health check passed",
            correlation_id=correlation_id,
        )

    return None


def _reconcile_deleted(
    environment: Environment,
    correlation_id: str,
) -> EnvironmentEvent | None:
    if environment.status == EnvironmentStatus.DELETED:
        return None

    if environment.status == EnvironmentStatus.DELETING:
        return transition_environment(
            environment,
            EnvironmentStatus.DELETED,
            actor="reconciler",
            reason="cleanup completed",
            correlation_id=correlation_id,
        )

    return transition_environment(
        environment,
        EnvironmentStatus.DELETING,
        actor="reconciler",
        reason="manual delete requested",
        correlation_id=correlation_id,
    )


def reconcile_environment(
    environment: Environment,
    correlation_id: str,
    now: datetime | None = None,
) -> EnvironmentEvent | None:
    """Apply at most one status transition toward the current desired state."""
    current_time = now if now is not None else datetime.now(timezone.utc)

    expired_event = expire_if_needed(environment, current_time, correlation_id)
    if expired_event is not None:
        return expired_event

    if environment.desired_state == DesiredState.ACTIVE:
        return _reconcile_active(environment, correlation_id)

    if environment.desired_state == DesiredState.DELETED:
        return _reconcile_deleted(environment, correlation_id)

    return None
