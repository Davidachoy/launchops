from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Response

from app.models import DesiredState, Environment, EnvironmentCreate, EnvironmentEvent
from app.state import events_by_environment

app = FastAPI(title="LaunchOps")

environments: dict[str, Environment] = {}
# Idempotency-Key -> (payload fingerprint, environment id)
idempotency_records: dict[str, tuple[str, str]] = {}


def _payload_fingerprint(payload: EnvironmentCreate) -> str:
    return payload.model_dump_json()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/environments", response_model=Environment)
def create_environment(
    payload: EnvironmentCreate,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Environment:
    fingerprint = _payload_fingerprint(payload)

    if idempotency_key is not None:
        key = idempotency_key.strip()
        if not key:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key must not be blank",
            )

        existing = idempotency_records.get(key)
        if existing is not None:
            stored_fingerprint, environment_id = existing
            if stored_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key reused with a different payload",
                )
            environment = environments[environment_id]
            response.status_code = 200
            return environment

    created_at = datetime.now(timezone.utc)
    environment = Environment(
        id=f"env-{uuid4()}",
        owner=payload.owner,
        repo=payload.repo,
        runtime=payload.runtime,
        resources=payload.resources,
        slo=payload.slo,
        ttl_hours=payload.ttl_hours,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=payload.ttl_hours),
    )
    environments[environment.id] = environment

    if idempotency_key is not None:
        idempotency_records[idempotency_key.strip()] = (fingerprint, environment.id)

    response.status_code = 201
    return environment


@app.get("/environments/{environment_id}", response_model=Environment)
def get_environment(environment_id: str) -> Environment:
    environment = environments.get(environment_id)
    if environment is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    return environment


@app.get(
    "/environments/{environment_id}/events",
    response_model=list[EnvironmentEvent],
)
def list_environment_events(environment_id: str) -> list[EnvironmentEvent]:
    if environment_id not in environments:
        raise HTTPException(status_code=404, detail="Environment not found")
    return events_by_environment.get(environment_id, [])


@app.delete("/environments/{environment_id}", response_model=Environment)
def delete_environment(environment_id: str) -> Environment:
    environment = environments.get(environment_id)
    if environment is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    if environment.desired_state == DesiredState.DELETED:
        return environment

    environment.desired_state = DesiredState.DELETED
    return environment
