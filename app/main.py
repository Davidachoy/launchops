from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.mappers import environment_create_to_row, row_to_environment, row_to_event
from app.db.models import Environment as EnvironmentRow
from app.db.models import EnvironmentEvent as EnvironmentEventRow
from app.db.models import IdempotencyRecord
from app.db.session import get_db
from app.models import DesiredState, Environment, EnvironmentCreate, EnvironmentEvent

app = FastAPI(title="LaunchOps")


def _payload_fingerprint(payload: EnvironmentCreate) -> str:
    return payload.model_dump_json()


def _environment_for_idempotency_key(
    db: Session,
    key: str,
    fingerprint: str,
) -> Environment:
    """Resolve an existing idempotency record into an Environment or raise 409."""
    existing = db.get(IdempotencyRecord, key)
    if existing is None:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key conflict could not be resolved",
        )
    if existing.request_fingerprint != fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key reused with a different payload",
        )
    row = db.get(EnvironmentRow, existing.environment_id)
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key refers to a missing environment",
        )
    return row_to_environment(row)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/environments", response_model=Environment)
def create_environment(
    payload: EnvironmentCreate,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Environment:
    fingerprint = _payload_fingerprint(payload)
    key: str | None = None

    if idempotency_key is not None:
        key = idempotency_key.strip()
        if not key:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key must not be blank",
            )

        existing = db.get(IdempotencyRecord, key)
        if existing is not None:
            environment = _environment_for_idempotency_key(db, key, fingerprint)
            response.status_code = 200
            return environment

    created_at = datetime.now(timezone.utc)
    environment_id = f"env-{uuid4()}"
    row = environment_create_to_row(
        payload,
        environment_id=environment_id,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=payload.ttl_hours),
    )
    db.add(row)

    if key is not None:
        db.add(
            IdempotencyRecord(
                idempotency_key=key,
                request_fingerprint=fingerprint,
                environment_id=environment_id,
                created_at=created_at,
            )
        )

    try:
        db.commit()
    except IntegrityError:
        # Concurrent create with the same Idempotency-Key: loser retries as a read.
        db.rollback()
        if key is None:
            raise
        environment = _environment_for_idempotency_key(db, key, fingerprint)
        response.status_code = 200
        return environment

    db.refresh(row)
    response.status_code = 201
    return row_to_environment(row)


@app.get("/environments/{environment_id}", response_model=Environment)
def get_environment(
    environment_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> Environment:
    row = db.get(EnvironmentRow, environment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    return row_to_environment(row)


@app.get(
    "/environments/{environment_id}/events",
    response_model=list[EnvironmentEvent],
)
def list_environment_events(
    environment_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> list[EnvironmentEvent]:
    if db.get(EnvironmentRow, environment_id) is None:
        raise HTTPException(status_code=404, detail="Environment not found")

    event_rows = db.scalars(
        select(EnvironmentEventRow)
        .where(EnvironmentEventRow.environment_id == environment_id)
        .order_by(EnvironmentEventRow.timestamp, EnvironmentEventRow.id)
    ).all()
    return [row_to_event(event_row) for event_row in event_rows]


@app.delete("/environments/{environment_id}", response_model=Environment)
def delete_environment(
    environment_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> Environment:
    row = db.get(EnvironmentRow, environment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    if row.desired_state == DesiredState.DELETED.value:
        return row_to_environment(row)

    row.desired_state = DesiredState.DELETED.value
    db.commit()
    db.refresh(row)
    return row_to_environment(row)
