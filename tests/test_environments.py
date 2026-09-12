from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db.models import Environment as EnvironmentRow
from app.db.models import EnvironmentEvent as EnvironmentEventRow
from app.db.models import IdempotencyRecord
from app.db.session import SessionLocal
from app.main import app
from app.models import EnvironmentStatus
from app.reconciler import reconcile_environment

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


@pytest.fixture
def client():
    _clear_database()
    with TestClient(app) as test_client:
        yield test_client
    _clear_database()


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_create_environment_returns_201_with_defaults(client: TestClient) -> None:
    response = client.post("/environments", json=CREATE_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("env-")
    assert body["status"] == "REQUESTED"
    assert body["desired_state"] == "ACTIVE"
    assert datetime.fromisoformat(body["expires_at"]) > datetime.fromisoformat(
        body["created_at"]
    )

    with SessionLocal() as db:
        row = db.get(EnvironmentRow, body["id"])
        assert row is not None
        assert row.owner == "alice"
        assert row.repo == "demo-app"
        assert row.cpu == "500m"
        assert row.memory == "512Mi"
        assert row.slo_availability == 0.99
        assert row.status == "REQUESTED"
        assert row.desired_state == "ACTIVE"


def test_get_existing_environment_returns_200(client: TestClient) -> None:
    created = client.post("/environments", json=CREATE_PAYLOAD).json()

    response = client.get(f"/environments/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_missing_environment_returns_404(client: TestClient) -> None:
    response = client.get("/environments/env-does-not-exist")

    assert response.status_code == 404


def test_delete_sets_desired_state_without_changing_status(
    client: TestClient,
) -> None:
    created = client.post("/environments", json=CREATE_PAYLOAD).json()

    response = client.delete(f"/environments/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["desired_state"] == "DELETED"
    assert body["status"] == "REQUESTED"

    with SessionLocal() as db:
        row = db.get(EnvironmentRow, created["id"])
        assert row is not None
        assert row.desired_state == "DELETED"
        assert row.status == "REQUESTED"


def test_delete_keeps_ready_status_when_marking_intent(client: TestClient) -> None:
    created = client.post("/environments", json=CREATE_PAYLOAD).json()
    env_id = created["id"]

    with SessionLocal() as db:
        row = db.get(EnvironmentRow, env_id)
        assert row is not None
        row.status = EnvironmentStatus.READY.value
        db.commit()

    response = client.delete(f"/environments/{env_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["desired_state"] == "DELETED"
    assert body["status"] == "READY"


def test_delete_missing_environment_returns_404(client: TestClient) -> None:
    response = client.delete("/environments/env-does-not-exist")

    assert response.status_code == 404


def test_delete_desired_state_is_idempotent(client: TestClient) -> None:
    created = client.post("/environments", json=CREATE_PAYLOAD).json()
    env_id = created["id"]

    first_delete = client.delete(f"/environments/{env_id}")
    assert first_delete.status_code == 200
    assert first_delete.json()["desired_state"] == "DELETED"
    assert first_delete.json()["status"] == "REQUESTED"

    second_delete = client.delete(f"/environments/{env_id}")
    assert second_delete.status_code == 200
    assert second_delete.json()["desired_state"] == "DELETED"
    assert second_delete.json()["status"] == "REQUESTED"

    with SessionLocal() as db:
        event_count = db.scalar(
            select(func.count())
            .select_from(EnvironmentEventRow)
            .where(EnvironmentEventRow.environment_id == env_id)
        )
        assert event_count == 0


@pytest.mark.parametrize(
    "override",
    [
        {"owner": ""},
        {"owner": "   "},
        {"ttl_hours": 0},
        {"ttl_hours": -5},
        {"slo": {"availability": 0}},
        {"slo": {"availability": 1.01}},
        {"resources": {"cpu": "", "memory": "512Mi"}},
        {"resources": {"cpu": "500m", "memory": "   "}},
    ],
    ids=[
        "blank_owner",
        "whitespace_owner",
        "zero_ttl",
        "negative_ttl",
        "zero_availability",
        "availability_above_one",
        "blank_cpu",
        "whitespace_memory",
    ],
)
def test_create_rejects_invalid_contract(
    client: TestClient, override: dict
) -> None:
    payload = {**CREATE_PAYLOAD, **override}

    response = client.post("/environments", json=payload)

    assert response.status_code == 422
    with SessionLocal() as db:
        count = db.scalar(select(func.count()).select_from(EnvironmentRow))
        assert count == 0


def test_list_events_missing_environment_returns_404(client: TestClient) -> None:
    response = client.get("/environments/env-does-not-exist/events")

    assert response.status_code == 404


def test_list_events_empty_history_returns_empty_list(client: TestClient) -> None:
    created = client.post("/environments", json=CREATE_PAYLOAD).json()

    response = client.get(f"/environments/{created['id']}/events")

    assert response.status_code == 200
    assert response.json() == []


def test_list_events_returns_ordered_history(client: TestClient) -> None:
    correlation_id = "corr-events-1"
    created = client.post("/environments", json=CREATE_PAYLOAD).json()
    env_id = created["id"]

    with SessionLocal() as db:
        reconcile_environment(db, env_id, correlation_id=correlation_id)
        reconcile_environment(db, env_id, correlation_id=correlation_id)
        reconcile_environment(db, env_id, correlation_id=correlation_id)

    response = client.get(f"/environments/{env_id}/events")

    assert response.status_code == 200
    events = response.json()
    assert [
        (event["from_status"], event["to_status"]) for event in events
    ] == [
        ("REQUESTED", "QUEUED"),
        ("QUEUED", "PROVISIONING"),
        ("PROVISIONING", "READY"),
    ]
    for event in events:
        assert event["environment_id"] == env_id
        assert event["actor"] == "reconciler"
        assert event["reason"]
        assert event["correlation_id"] == correlation_id
        assert event["timestamp"]
    assert events[0]["timestamp"] <= events[1]["timestamp"] <= events[2]["timestamp"]


def test_create_with_same_idempotency_key_and_payload_returns_same_environment(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "create-demo-1"}

    first = client.post("/environments", json=CREATE_PAYLOAD, headers=headers)
    second = client.post("/environments", json=CREATE_PAYLOAD, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    with SessionLocal() as db:
        env_count = db.scalar(select(func.count()).select_from(EnvironmentRow))
        key_count = db.scalar(select(func.count()).select_from(IdempotencyRecord))
        assert env_count == 1
        assert key_count == 1
        record = db.get(IdempotencyRecord, "create-demo-1")
        assert record is not None
        assert record.environment_id == first.json()["id"]


def test_create_with_same_idempotency_key_and_different_payload_conflicts(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "create-demo-1"}
    first = client.post("/environments", json=CREATE_PAYLOAD, headers=headers)
    assert first.status_code == 201

    conflicting = {**CREATE_PAYLOAD, "repo": "other-app"}
    second = client.post("/environments", json=conflicting, headers=headers)

    assert second.status_code == 409
    with SessionLocal() as db:
        env_count = db.scalar(select(func.count()).select_from(EnvironmentRow))
        key_count = db.scalar(select(func.count()).select_from(IdempotencyRecord))
        assert env_count == 1
        assert key_count == 1
        row = db.get(EnvironmentRow, first.json()["id"])
        assert row is not None
        assert row.repo == "demo-app"


def test_create_rejects_blank_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/environments",
        json=CREATE_PAYLOAD,
        headers={"Idempotency-Key": "   "},
    )

    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(EnvironmentRow)) == 0
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


def test_create_without_idempotency_key_allows_distinct_environments(
    client: TestClient,
) -> None:
    first = client.post("/environments", json=CREATE_PAYLOAD)
    second = client.post("/environments", json=CREATE_PAYLOAD)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(EnvironmentRow)) == 2
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


def test_concurrent_idempotent_creates_persist_single_environment() -> None:
    """Two racing clients with the same key must leave exactly one env row."""
    _clear_database()
    key = "race-idempotency-1"
    headers = {"Idempotency-Key": key}

    def _create(_: int):
        with TestClient(app) as client:
            return client.post("/environments", json=CREATE_PAYLOAD, headers=headers)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(_create, range(8)))

        assert all(response.status_code in {200, 201} for response in responses)
        assert sum(1 for response in responses if response.status_code == 201) >= 1
        environment_ids = {response.json()["id"] for response in responses}
        assert len(environment_ids) == 1

        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(EnvironmentRow)) == 1
            assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
            record = db.get(IdempotencyRecord, key)
            assert record is not None
            assert record.environment_id == next(iter(environment_ids))
    finally:
        _clear_database()


def test_state_survives_app_restart(client: TestClient) -> None:
    headers = {"Idempotency-Key": "restart-1"}
    created = client.post("/environments", json=CREATE_PAYLOAD, headers=headers)
    assert created.status_code == 201
    env_id = created.json()["id"]

    # New TestClient = fresh process-like boundary; only PostgreSQL retains state.
    with TestClient(app) as restarted:
        fetched = restarted.get(f"/environments/{env_id}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == env_id
        assert fetched.json()["status"] == "REQUESTED"

        replay = restarted.post(
            "/environments",
            json=CREATE_PAYLOAD,
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == env_id

        events = restarted.get(f"/environments/{env_id}/events")
        assert events.status_code == 200
        assert events.json() == []


def test_reconcile_and_ttl_survive_app_restart(client: TestClient) -> None:
    created = client.post("/environments", json=CREATE_PAYLOAD).json()
    env_id = created["id"]
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires_at = created_at + timedelta(hours=2)
    now = expires_at

    with SessionLocal() as db:
        row = db.get(EnvironmentRow, env_id)
        assert row is not None
        row.status = EnvironmentStatus.READY.value
        row.created_at = created_at
        row.expires_at = expires_at
        db.commit()

    with TestClient(app) as restarted:
        with SessionLocal() as db:
            event = reconcile_environment(
                db, env_id, correlation_id="corr-restart-ttl", now=now
            )
            assert event is not None
            row = db.get(EnvironmentRow, env_id)
            assert row is not None
            assert row.status == EnvironmentStatus.EXPIRED.value
            assert row.desired_state == "DELETED"

        fetched = restarted.get(f"/environments/{env_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "EXPIRED"
        assert fetched.json()["desired_state"] == "DELETED"

        events = restarted.get(f"/environments/{env_id}/events")
        assert events.status_code == 200
        body = events.json()
        assert len(body) == 1
        assert body[0]["from_status"] == "READY"
        assert body[0]["to_status"] == "EXPIRED"
        assert body[0]["reason"] == "ttl expired"