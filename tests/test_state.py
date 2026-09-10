from datetime import datetime, timedelta, timezone

import pytest

from app.models import Environment, EnvironmentStatus, ResourceSpec, SLOSpec
from app.state import events_by_environment, transition_environment


@pytest.fixture(autouse=True)
def clear_events():
    events_by_environment.clear()
    yield
    events_by_environment.clear()


def make_environment(status: EnvironmentStatus) -> Environment:
    now = datetime.now(timezone.utc)
    return Environment(
        id="env-test",
        owner="alice",
        repo="demo-app",
        runtime="python:3.12",
        resources=ResourceSpec(cpu="500m", memory="512Mi"),
        slo=SLOSpec(availability=0.99),
        ttl_hours=2,
        status=status,
        created_at=now,
        expires_at=now + timedelta(hours=2),
    )


def test_requested_to_queued_is_allowed_and_records_event() -> None:
    environment = make_environment(EnvironmentStatus.REQUESTED)

    event = transition_environment(
        environment,
        EnvironmentStatus.QUEUED,
        actor="reconciler",
        reason="accepted for provisioning",
        correlation_id="corr-1",
    )

    assert environment.status == EnvironmentStatus.QUEUED
    assert event.environment_id == environment.id
    assert event.from_status == EnvironmentStatus.REQUESTED
    assert event.to_status == EnvironmentStatus.QUEUED
    assert event.actor == "reconciler"
    assert event.reason == "accepted for provisioning"
    assert event.correlation_id == "corr-1"
    assert events_by_environment[environment.id] == [event]


def test_provisioning_to_ready_is_allowed() -> None:
    environment = make_environment(EnvironmentStatus.PROVISIONING)

    event = transition_environment(
        environment,
        EnvironmentStatus.READY,
        actor="reconciler",
        reason="provisioning succeeded",
        correlation_id="corr-2",
    )

    assert environment.status == EnvironmentStatus.READY
    assert event.from_status == EnvironmentStatus.PROVISIONING
    assert event.to_status == EnvironmentStatus.READY
    assert events_by_environment[environment.id] == [event]


def test_provisioning_to_failed_is_allowed() -> None:
    environment = make_environment(EnvironmentStatus.PROVISIONING)

    event = transition_environment(
        environment,
        EnvironmentStatus.FAILED,
        actor="reconciler",
        reason="provisioning failed",
        correlation_id="corr-3",
    )

    assert environment.status == EnvironmentStatus.FAILED
    assert event.from_status == EnvironmentStatus.PROVISIONING
    assert event.to_status == EnvironmentStatus.FAILED
    assert events_by_environment[environment.id] == [event]


def test_requested_to_ready_is_rejected_without_side_effects() -> None:
    environment = make_environment(EnvironmentStatus.REQUESTED)

    with pytest.raises(ValueError, match="invalid transition: REQUESTED -> READY"):
        transition_environment(
            environment,
            EnvironmentStatus.READY,
            actor="reconciler",
            reason="skip ahead",
            correlation_id="corr-4",
        )

    assert environment.status == EnvironmentStatus.REQUESTED
    assert environment.id not in events_by_environment


def test_ready_to_deleting_is_allowed_for_manual_delete() -> None:
    environment = make_environment(EnvironmentStatus.READY)

    event = transition_environment(
        environment,
        EnvironmentStatus.DELETING,
        actor="reconciler",
        reason="manual delete requested",
        correlation_id="corr-5",
    )

    assert environment.status == EnvironmentStatus.DELETING
    assert event.from_status == EnvironmentStatus.READY
    assert event.to_status == EnvironmentStatus.DELETING
    assert len(events_by_environment[environment.id]) == 1


@pytest.mark.parametrize(
    "status",
    [
        EnvironmentStatus.REQUESTED,
        EnvironmentStatus.QUEUED,
        EnvironmentStatus.PROVISIONING,
    ],
)
def test_in_flight_to_deleting_is_allowed(status: EnvironmentStatus) -> None:
    environment = make_environment(status)

    event = transition_environment(
        environment,
        EnvironmentStatus.DELETING,
        actor="reconciler",
        reason="manual delete requested",
        correlation_id="corr-6",
    )

    assert environment.status == EnvironmentStatus.DELETING
    assert event.from_status == status
    assert event.to_status == EnvironmentStatus.DELETING
