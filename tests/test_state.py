from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app.db.models import Environment as EnvironmentRow
from app.db.models import EnvironmentEvent as EnvironmentEventRow
from app.db.models import IdempotencyRecord
from app.db.session import SessionLocal
from app.models import DesiredState, EnvironmentStatus
from app.state import transition_environment


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
) -> EnvironmentRow:
    now = datetime.now(timezone.utc)
    row = EnvironmentRow(
        id="env-test",
        owner="alice",
        repo="demo-app",
        runtime="python:3.12",
        cpu="500m",
        memory="512Mi",
        slo_availability=0.99,
        ttl_hours=2,
        desired_state=desired_state.value,
        status=status.value,
        created_at=now,
        expires_at=now + timedelta(hours=2),
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


def test_requested_to_queued_is_allowed_and_records_event() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.REQUESTED)

        event = transition_environment(
            db,
            row,
            EnvironmentStatus.QUEUED,
            actor="reconciler",
            reason="accepted for provisioning",
            correlation_id="corr-1",
        )
        db.commit()

        assert row.status == EnvironmentStatus.QUEUED.value
        assert event.environment_id == row.id
        assert event.from_status == EnvironmentStatus.REQUESTED
        assert event.to_status == EnvironmentStatus.QUEUED
        assert event.actor == "reconciler"
        assert event.reason == "accepted for provisioning"
        assert event.correlation_id == "corr-1"
        events = list_events(db, row.id)
        assert len(events) == 1
        assert events[0].to_status == EnvironmentStatus.QUEUED.value


def test_provisioning_to_ready_is_allowed() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.PROVISIONING)

        event = transition_environment(
            db,
            row,
            EnvironmentStatus.READY,
            actor="reconciler",
            reason="provisioning succeeded",
            correlation_id="corr-2",
        )
        db.commit()

        assert row.status == EnvironmentStatus.READY.value
        assert event.from_status == EnvironmentStatus.PROVISIONING
        assert event.to_status == EnvironmentStatus.READY
        assert len(list_events(db, row.id)) == 1


def test_provisioning_to_failed_is_allowed() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.PROVISIONING)

        event = transition_environment(
            db,
            row,
            EnvironmentStatus.FAILED,
            actor="reconciler",
            reason="provisioning failed",
            correlation_id="corr-3",
        )
        db.commit()

        assert row.status == EnvironmentStatus.FAILED.value
        assert event.from_status == EnvironmentStatus.PROVISIONING
        assert event.to_status == EnvironmentStatus.FAILED
        assert len(list_events(db, row.id)) == 1


def test_requested_to_ready_is_rejected_without_side_effects() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.REQUESTED)

        with pytest.raises(ValueError, match="invalid transition: REQUESTED -> READY"):
            transition_environment(
                db,
                row,
                EnvironmentStatus.READY,
                actor="reconciler",
                reason="skip ahead",
                correlation_id="corr-4",
            )
        db.rollback()

        db.refresh(row)
        assert row.status == EnvironmentStatus.REQUESTED.value
        count = db.scalar(
            select(func.count())
            .select_from(EnvironmentEventRow)
            .where(EnvironmentEventRow.environment_id == row.id)
        )
        assert count == 0


def test_ready_to_deleting_is_allowed_for_manual_delete() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.READY)

        event = transition_environment(
            db,
            row,
            EnvironmentStatus.DELETING,
            actor="reconciler",
            reason="manual delete requested",
            correlation_id="corr-5",
        )
        db.commit()

        assert row.status == EnvironmentStatus.DELETING.value
        assert event.from_status == EnvironmentStatus.READY
        assert event.to_status == EnvironmentStatus.DELETING
        assert len(list_events(db, row.id)) == 1


@pytest.mark.parametrize(
    "status",
    [
        EnvironmentStatus.REQUESTED,
        EnvironmentStatus.QUEUED,
        EnvironmentStatus.PROVISIONING,
    ],
)
def test_in_flight_to_deleting_is_allowed(status: EnvironmentStatus) -> None:
    with SessionLocal() as db:
        row = make_row(db, status)

        event = transition_environment(
            db,
            row,
            EnvironmentStatus.DELETING,
            actor="reconciler",
            reason="manual delete requested",
            correlation_id="corr-6",
        )
        db.commit()

        assert row.status == EnvironmentStatus.DELETING.value
        assert event.from_status == status
        assert event.to_status == EnvironmentStatus.DELETING


def test_status_and_event_commit_atomically() -> None:
    with SessionLocal() as db:
        row = make_row(db, EnvironmentStatus.REQUESTED)
        transition_environment(
            db,
            row,
            EnvironmentStatus.QUEUED,
            actor="reconciler",
            reason="accepted for provisioning",
            correlation_id="corr-atomic",
        )
        db.rollback()

        db.refresh(row)
        assert row.status == EnvironmentStatus.REQUESTED.value
        assert list_events(db, row.id) == []

        transition_environment(
            db,
            row,
            EnvironmentStatus.QUEUED,
            actor="reconciler",
            reason="accepted for provisioning",
            correlation_id="corr-atomic",
        )
        db.commit()

    with SessionLocal() as db:
        row = db.get(EnvironmentRow, "env-test")
        assert row is not None
        assert row.status == EnvironmentStatus.QUEUED.value
        events = list_events(db, row.id)
        assert len(events) == 1
        assert events[0].to_status == EnvironmentStatus.QUEUED.value
