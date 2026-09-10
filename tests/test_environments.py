from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app, environments, idempotency_records
from app.models import EnvironmentStatus
from app.reconciler import reconcile_environment
from app.state import events_by_environment

CREATE_PAYLOAD = {
    "owner": "alice",
    "repo": "demo-app",
    "runtime": "python:3.12",
    "resources": {"cpu": "500m", "memory": "512Mi"},
    "slo": {"availability": 0.99},
    "ttl_hours": 2,
}


@pytest.fixture
def client():
    environments.clear()
    events_by_environment.clear()
    idempotency_records.clear()
    with TestClient(app) as test_client:
        yield test_client
    environments.clear()
    events_by_environment.clear()
    idempotency_records.clear()


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


def test_delete_keeps_ready_status_when_marking_intent(client: TestClient) -> None:
    created = client.post("/environments", json=CREATE_PAYLOAD).json()
    env_id = created["id"]
    environments[env_id].status = EnvironmentStatus.READY

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
    assert events_by_environment.get(env_id, []) == []


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
    assert environments == {}


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
    environment = environments[env_id]

    reconcile_environment(environment, correlation_id=correlation_id)
    reconcile_environment(environment, correlation_id=correlation_id)
    reconcile_environment(environment, correlation_id=correlation_id)

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
    assert len(environments) == 1


def test_create_with_same_idempotency_key_and_different_payload_conflicts(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "create-demo-1"}
    first = client.post("/environments", json=CREATE_PAYLOAD, headers=headers)
    assert first.status_code == 201

    conflicting = {**CREATE_PAYLOAD, "repo": "other-app"}
    second = client.post("/environments", json=conflicting, headers=headers)

    assert second.status_code == 409
    assert len(environments) == 1
    assert environments[first.json()["id"]].repo == "demo-app"
