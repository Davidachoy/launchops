from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models import Environment as EnvironmentRow
from app.db.models import EnvironmentEvent as EnvironmentEventRow
from app.db.models import IdempotencyRecord
from app.db.session import SessionLocal
from app.main import app
from app.models import DesiredState, EnvironmentStatus
from app.reconciler import expire_if_needed, reconcile_environment

CREATE_PAYLOAD = {
    "owner": "alice",
    "repo": "demo-app",
    "runtime": "python:3.12",
    "resources": {"cpu": "500m", "memory": "512Mi"},
    "slo": {"availability": 0.99},
    "ttl_hours": 2,
}


def _clear_database() -> None:
    with SessionLocal() as db:
        db.execute(delete(IdempotencyRecord))
        db.execute(delete(EnvironmentEventRow))
        db.execute(delete(EnvironmentRow))
        db.commit()


@pytest.fixture(autouse=True)
def clear_database():
    _clear_database()
    yield
    _clear_database()


def make_row(
    db,
    status: EnvironmentStatus,
    desired_state: DesiredState = DesiredState.ACTIVE,
    *,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    environment_id: str = "env-test",
) -> EnvironmentRow:
    now = datetime.now(timezone.utc)
    created = created_at or now
    expires = expires_at or (created + timedelta(hours=2))
    row = EnvironmentRow(
        id=environment_id,
        owner="alice",
        repo="demo-app",
        runtime="python:3.12",
        cpu="500m",
        memory="512Mi",
        slo_availability=0.99,
        ttl_hours=2,
        desired_state=desired_state.value,
        status=status.value,
        created_at=created,
        expires_at=expires,
        last_error=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_events(db, environment_id: str) -> list[EnvironmentEventRow]:
    return list(
        db.scalars(
            select(EnvironmentEventRow)
            .where(EnvironmentEventRow.environment_id == environment_id)
            .order_by(EnvironmentEventRow.timestamp, EnvironmentEventRow.id)
        ).all()
    )


def load_row(db, environment_id: str) -> EnvironmentRow:
    row = db.get(EnvironmentRow, environment_id)
    assert row is not None
    return row


def test_reconcile_requested_to_queued() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.REQUESTED)

        event = reconcile_environment(db, row.id, correlation_id="corr-1")
        row = load_row(db, row.id)

        assert row.status == EnvironmentStatus.QUEUED.value
        assert event is not None
        assert event.from_status == EnvironmentStatus.REQUESTED
        assert event.to_status == EnvironmentStatus.QUEUED
        assert len(list_events(db, row.id)) == 1


def test_reconcile_queued_to_provisioning() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.QUEUED)

        event = reconcile_environment(db, row.id, correlation_id="corr-2")
        row = load_row(db, row.id)

        assert row.status == EnvironmentStatus.PROVISIONING.value
        assert event is not None
        assert event.from_status == EnvironmentStatus.QUEUED
        assert event.to_status == EnvironmentStatus.PROVISIONING
        assert len(list_events(db, row.id)) == 1


def test_reconcile_provisioning_to_ready() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.PROVISIONING)

        event = reconcile_environment(db, row.id, correlation_id="corr-3")
        row = load_row(db, row.id)

        assert row.status == EnvironmentStatus.READY.value
        assert event is not None
        assert event.from_status == EnvironmentStatus.PROVISIONING
        assert event.to_status == EnvironmentStatus.READY
        assert len(list_events(db, row.id)) == 1


def test_reconcile_ready_is_noop() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.READY)

        event = reconcile_environment(db, row.id, correlation_id="corr-4")
        row = load_row(db, row.id)

        assert event is None
        assert row.status == EnvironmentStatus.READY.value
        assert list_events(db, row.id) == []


def test_reconcile_missing_environment_raises_and_rolls_back() -> None:
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="environment not found: env-missing"):
            reconcile_environment(db, "env-missing", correlation_id="corr-missing")

        assert list_events(db, "env-missing") == []


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
    with SessionLocal() as db:
        row = make_row(db, status, desired_state=DesiredState.DELETED)

        event = reconcile_environment(
            db, row.id, correlation_id="corr-delete-abort"
        )
        row = load_row(db, row.id)

        assert row.status == EnvironmentStatus.DELETING.value
        assert event is not None
        assert event.from_status == status
        assert event.to_status == EnvironmentStatus.DELETING
        assert len(list_events(db, row.id)) == 1


def test_reconcile_deleted_deleting_to_deleted() -> None:
    with SessionLocal() as db:
        row = make_row(
            db,
            EnvironmentStatus.DELETING,
            desired_state=DesiredState.DELETED,
        )

        event = reconcile_environment(db, row.id, correlation_id="corr-delete-2")
        row = load_row(db, row.id)

        assert row.status == EnvironmentStatus.DELETED.value
        assert event is not None
        assert event.from_status == EnvironmentStatus.DELETING
        assert event.to_status == EnvironmentStatus.DELETED
        assert len(list_events(db, row.id)) == 1


def test_reconcile_deleted_terminal_is_noop() -> None:
    with SessionLocal() as db:
        row = make_row(
            db,
            EnvironmentStatus.DELETED,
            desired_state=DesiredState.DELETED,
        )

        event = reconcile_environment(db, row.id, correlation_id="corr-delete-3")
        row = load_row(db, row.id)

        assert event is None
        assert row.status == EnvironmentStatus.DELETED.value
        assert list_events(db, row.id) == []


def test_expire_if_needed_ready_with_elapsed_ttl() -> None:
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = expires_at

    with SessionLocal() as db:
        row = make_row(
            db,
            EnvironmentStatus.READY,
            created_at=created_at,
            expires_at=expires_at,
        )

        event = expire_if_needed(db, row, now=now, correlation_id="corr-expire-1")
        db.commit()

        assert row.status == EnvironmentStatus.EXPIRED.value
        assert row.desired_state == DesiredState.DELETED.value
        assert event is not None
        assert event.from_status == EnvironmentStatus.READY
        assert event.to_status == EnvironmentStatus.EXPIRED
        assert len(list_events(db, row.id)) == 1


def test_expire_if_needed_ready_with_remaining_ttl_is_noop() -> None:
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = created_at + timedelta(hours=1)

    with SessionLocal() as db:
        row = make_row(
            db,
            EnvironmentStatus.READY,
            created_at=created_at,
            expires_at=expires_at,
        )

        event = expire_if_needed(db, row, now=now, correlation_id="corr-expire-2")
        db.commit()

        assert event is None
        assert row.status == EnvironmentStatus.READY.value
        assert row.desired_state == DesiredState.ACTIVE.value
        assert list_events(db, row.id) == []


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

    with SessionLocal() as db:
        row = make_row(
            db,
            status,
            created_at=created_at,
            expires_at=expires_at,
        )

        event = expire_if_needed(
            db, row, now=now, correlation_id="corr-expire-pending"
        )
        db.commit()

        assert row.status == EnvironmentStatus.DELETING.value
        assert row.desired_state == DesiredState.DELETED.value
        assert event is not None
        assert event.from_status == status
        assert event.to_status == EnvironmentStatus.DELETING
        assert len(list_events(db, row.id)) == 1


def test_reconcile_uses_persisted_expires_at_for_ttl() -> None:
    correlation_id = "corr-ttl-row"
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = expires_at

    with SessionLocal() as db:
        row = make_row(
            db,
            EnvironmentStatus.READY,
            created_at=created_at,
            expires_at=expires_at,
        )

        event = reconcile_environment(
            db, row.id, correlation_id=correlation_id, now=now
        )
        row = load_row(db, row.id)

        assert event is not None
        assert row.status == EnvironmentStatus.EXPIRED.value
        assert row.desired_state == DesiredState.DELETED.value
        assert row.expires_at == expires_at


def test_reconcile_expires_ready_then_cleans_up_over_three_calls() -> None:
    correlation_id = "corr-ttl-1"
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = expires_at

    with SessionLocal() as db:
        row = make_row(
            db,
            EnvironmentStatus.READY,
            created_at=created_at,
            expires_at=expires_at,
        )
        env_id = row.id

        first = reconcile_environment(
            db, env_id, correlation_id=correlation_id, now=now
        )
        assert first is not None
        assert load_row(db, env_id).status == EnvironmentStatus.EXPIRED.value
        assert load_row(db, env_id).desired_state == DesiredState.DELETED.value

        second = reconcile_environment(
            db, env_id, correlation_id=correlation_id, now=now
        )
        assert second is not None
        assert load_row(db, env_id).status == EnvironmentStatus.DELETING.value

        third = reconcile_environment(
            db, env_id, correlation_id=correlation_id, now=now
        )
        assert third is not None
        assert load_row(db, env_id).status == EnvironmentStatus.DELETED.value

        history = [
            (EnvironmentStatus(event.from_status), EnvironmentStatus(event.to_status))
            for event in list_events(db, env_id)
        ]
        assert history == [
            (EnvironmentStatus.READY, EnvironmentStatus.EXPIRED),
            (EnvironmentStatus.EXPIRED, EnvironmentStatus.DELETING),
            (EnvironmentStatus.DELETING, EnvironmentStatus.DELETED),
        ]
        assert {event.correlation_id for event in list_events(db, env_id)} == {
            correlation_id
        }


def test_create_then_three_reconciles_reach_ready() -> None:
    correlation_id = "corr-create-1"

    with TestClient(app) as client:
        created = client.post("/environments", json=CREATE_PAYLOAD)
        assert created.status_code == 201
        env_id = created.json()["id"]
        assert created.json()["status"] == "REQUESTED"

    with SessionLocal() as db:
        reconcile_environment(db, env_id, correlation_id=correlation_id)
        assert load_row(db, env_id).status == EnvironmentStatus.QUEUED.value

        reconcile_environment(db, env_id, correlation_id=correlation_id)
        assert load_row(db, env_id).status == EnvironmentStatus.PROVISIONING.value

        reconcile_environment(db, env_id, correlation_id=correlation_id)
        assert load_row(db, env_id).status == EnvironmentStatus.READY.value

        events = list_events(db, env_id)
        history = [
            (EnvironmentStatus(event.from_status), EnvironmentStatus(event.to_status))
            for event in events
        ]
        assert history == [
            (EnvironmentStatus.REQUESTED, EnvironmentStatus.QUEUED),
            (EnvironmentStatus.QUEUED, EnvironmentStatus.PROVISIONING),
            (EnvironmentStatus.PROVISIONING, EnvironmentStatus.READY),
        ]
        assert {event.correlation_id for event in events} == {correlation_id}


def test_ready_state_and_event_history_survive_app_restart() -> None:
    """Lab 1 persistence proof: READY + event history outlive process restart."""
    correlation_id = "corr-lab1-restart"

    with TestClient(app) as client:
        created = client.post("/environments", json=CREATE_PAYLOAD)
        assert created.status_code == 201
        env_id = created.json()["id"]

    with SessionLocal() as db:
        reconcile_environment(db, env_id, correlation_id=correlation_id)
        reconcile_environment(db, env_id, correlation_id=correlation_id)
        reconcile_environment(db, env_id, correlation_id=correlation_id)
        assert load_row(db, env_id).status == EnvironmentStatus.READY.value

    # New TestClient ≈ apagar LaunchOps y volverlo a iniciar.
    with TestClient(app) as restarted:
        fetched = restarted.get(f"/environments/{env_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "READY"
        assert fetched.json()["desired_state"] == "ACTIVE"

        events = restarted.get(f"/environments/{env_id}/events")
        assert events.status_code == 200
        body = events.json()
        assert [
            (event["from_status"], event["to_status"]) for event in body
        ] == [
            ("REQUESTED", "QUEUED"),
            ("QUEUED", "PROVISIONING"),
            ("PROVISIONING", "READY"),
        ]
        assert {event["correlation_id"] for event in body} == {correlation_id}
