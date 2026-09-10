from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, environments, idempotency_records
from app.models import DesiredState, Environment, EnvironmentStatus, ResourceSpec, SLOSpec
from app.reconciler import expire_if_needed, reconcile_environment
from app.state import events_by_environment

CREATE_PAYLOAD = {
    "owner": "alice",
    "repo": "demo-app",
    "runtime": "python:3.12",
    "resources": {"cpu": "500m", "memory": "512Mi"},
    "slo": {"availability": 0.99},
    "ttl_hours": 2,
}


@pytest.fixture(autouse=True)
def clear_stores():
    environments.clear()
    events_by_environment.clear()
    idempotency_records.clear()
    yield
    environments.clear()
    events_by_environment.clear()
    idempotency_records.clear()


def make_environment(
    status: EnvironmentStatus,
    desired_state: DesiredState = DesiredState.ACTIVE,
    *,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Environment:
    now = datetime.now(timezone.utc)
    created = created_at or now
    expires = expires_at or (created + timedelta(hours=2))
    return Environment(
        id="env-test",
        owner="alice",
        repo="demo-app",
        runtime="python:3.12",
        resources=ResourceSpec(cpu="500m", memory="512Mi"),
        slo=SLOSpec(availability=0.99),
        ttl_hours=2,
        desired_state=desired_state,
        status=status,
        created_at=created,
        expires_at=expires,
    )


def test_reconcile_requested_to_queued() -> None:
    environment = make_environment(EnvironmentStatus.REQUESTED)

    event = reconcile_environment(environment, correlation_id="corr-1")

    assert environment.status == EnvironmentStatus.QUEUED
    assert event is not None
    assert event.from_status == EnvironmentStatus.REQUESTED
    assert event.to_status == EnvironmentStatus.QUEUED
    assert len(events_by_environment[environment.id]) == 1


def test_reconcile_queued_to_provisioning() -> None:
    environment = make_environment(EnvironmentStatus.QUEUED)

    event = reconcile_environment(environment, correlation_id="corr-2")

    assert environment.status == EnvironmentStatus.PROVISIONING
    assert event is not None
    assert event.from_status == EnvironmentStatus.QUEUED
    assert event.to_status == EnvironmentStatus.PROVISIONING
    assert len(events_by_environment[environment.id]) == 1


def test_reconcile_provisioning_to_ready() -> None:
    environment = make_environment(EnvironmentStatus.PROVISIONING)

    event = reconcile_environment(environment, correlation_id="corr-3")

    assert environment.status == EnvironmentStatus.READY
    assert event is not None
    assert event.from_status == EnvironmentStatus.PROVISIONING
    assert event.to_status == EnvironmentStatus.READY
    assert len(events_by_environment[environment.id]) == 1


def test_reconcile_ready_is_noop() -> None:
    environment = make_environment(EnvironmentStatus.READY)

    event = reconcile_environment(environment, correlation_id="corr-4")

    assert event is None
    assert environment.status == EnvironmentStatus.READY
    assert environment.id not in events_by_environment


@pytest.mark.parametrize(
    "status",
    [
        EnvironmentStatus.REQUESTED,
        EnvironmentStatus.QUEUED,
        EnvironmentStatus.PROVISIONING,
        EnvironmentStatus.READY,
        EnvironmentStatus.EXPIRED,
    ],
)
def test_reconcile_deleted_moves_in_flight_to_deleting(
    status: EnvironmentStatus,
) -> None:
    environment = make_environment(status, desired_state=DesiredState.DELETED)

    event = reconcile_environment(environment, correlation_id="corr-delete-abort")

    assert environment.status == EnvironmentStatus.DELETING
    assert event is not None
    assert event.from_status == status
    assert event.to_status == EnvironmentStatus.DELETING
    assert len(events_by_environment[environment.id]) == 1


def test_reconcile_deleted_deleting_to_deleted() -> None:
    environment = make_environment(
        EnvironmentStatus.DELETING,
        desired_state=DesiredState.DELETED,
    )

    event = reconcile_environment(environment, correlation_id="corr-delete-2")

    assert environment.status == EnvironmentStatus.DELETED
    assert event is not None
    assert event.from_status == EnvironmentStatus.DELETING
    assert event.to_status == EnvironmentStatus.DELETED
    assert len(events_by_environment[environment.id]) == 1


def test_reconcile_deleted_terminal_is_noop() -> None:
    environment = make_environment(
        EnvironmentStatus.DELETED,
        desired_state=DesiredState.DELETED,
    )

    event = reconcile_environment(environment, correlation_id="corr-delete-3")

    assert event is None
    assert environment.status == EnvironmentStatus.DELETED
    assert environment.id not in events_by_environment


def test_expire_if_needed_ready_with_elapsed_ttl() -> None:
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = expires_at
    environment = make_environment(
        EnvironmentStatus.READY,
        created_at=created_at,
        expires_at=expires_at,
    )

    event = expire_if_needed(environment, now=now, correlation_id="corr-expire-1")

    assert environment.status == EnvironmentStatus.EXPIRED
    assert environment.desired_state == DesiredState.DELETED
    assert event is not None
    assert event.from_status == EnvironmentStatus.READY
    assert event.to_status == EnvironmentStatus.EXPIRED
    assert len(events_by_environment[environment.id]) == 1


def test_expire_if_needed_ready_with_remaining_ttl_is_noop() -> None:
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = created_at + timedelta(hours=1)
    environment = make_environment(
        EnvironmentStatus.READY,
        created_at=created_at,
        expires_at=expires_at,
    )

    event = expire_if_needed(environment, now=now, correlation_id="corr-expire-2")

    assert event is None
    assert environment.status == EnvironmentStatus.READY
    assert environment.desired_state == DesiredState.ACTIVE
    assert environment.id not in events_by_environment


@pytest.mark.parametrize(
    "status",
    [
        EnvironmentStatus.REQUESTED,
        EnvironmentStatus.QUEUED,
        EnvironmentStatus.PROVISIONING,
    ],
)
def test_expire_if_needed_pending_operation_with_elapsed_ttl(
    status: EnvironmentStatus,
) -> None:
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = expires_at
    environment = make_environment(
        status,
        created_at=created_at,
        expires_at=expires_at,
    )

    event = expire_if_needed(environment, now=now, correlation_id="corr-expire-pending")

    assert environment.status == EnvironmentStatus.DELETING
    assert environment.desired_state == DesiredState.DELETED
    assert event is not None
    assert event.from_status == status
    assert event.to_status == EnvironmentStatus.DELETING
    assert len(events_by_environment[environment.id]) == 1


def test_reconcile_expires_ready_then_cleans_up_over_three_calls() -> None:
    correlation_id = "corr-ttl-1"
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = expires_at
    environment = make_environment(
        EnvironmentStatus.READY,
        created_at=created_at,
        expires_at=expires_at,
    )

    first = reconcile_environment(environment, correlation_id=correlation_id, now=now)
    assert first is not None
    assert environment.status == EnvironmentStatus.EXPIRED
    assert environment.desired_state == DesiredState.DELETED

    second = reconcile_environment(environment, correlation_id=correlation_id, now=now)
    assert second is not None
    assert environment.status == EnvironmentStatus.DELETING

    third = reconcile_environment(environment, correlation_id=correlation_id, now=now)
    assert third is not None
    assert environment.status == EnvironmentStatus.DELETED

    history = [
        (event.from_status, event.to_status)
        for event in events_by_environment[environment.id]
    ]
    assert history == [
        (EnvironmentStatus.READY, EnvironmentStatus.EXPIRED),
        (EnvironmentStatus.EXPIRED, EnvironmentStatus.DELETING),
        (EnvironmentStatus.DELETING, EnvironmentStatus.DELETED),
    ]
    assert {event.correlation_id for event in events_by_environment[environment.id]} == {
        correlation_id
    }


def test_create_then_three_reconciles_reach_ready() -> None:
    correlation_id = "corr-create-1"

    with TestClient(app) as client:
        created = client.post("/environments", json=CREATE_PAYLOAD)
        assert created.status_code == 201
        env_id = created.json()["id"]
        assert created.json()["status"] == "REQUESTED"

        environment = environments[env_id]

        reconcile_environment(environment, correlation_id=correlation_id)
        assert environment.status == EnvironmentStatus.QUEUED

        reconcile_environment(environment, correlation_id=correlation_id)
        assert environment.status == EnvironmentStatus.PROVISIONING

        reconcile_environment(environment, correlation_id=correlation_id)
        assert environment.status == EnvironmentStatus.READY

        events = events_by_environment[env_id]
        history = [(event.from_status, event.to_status) for event in events]
        assert history == [
            (EnvironmentStatus.REQUESTED, EnvironmentStatus.QUEUED),
            (EnvironmentStatus.QUEUED, EnvironmentStatus.PROVISIONING),
            (EnvironmentStatus.PROVISIONING, EnvironmentStatus.READY),
        ]
        assert {event.correlation_id for event in events} == {correlation_id}
