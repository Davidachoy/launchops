from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class EnvironmentStatus(str, Enum):
    REQUESTED = "REQUESTED"
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class DesiredState(str, Enum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"


class ResourceSpec(BaseModel):
    cpu: str = Field(min_length=1)
    memory: str = Field(min_length=1)

    @field_validator("cpu", "memory")
    @classmethod
    def non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class SLOSpec(BaseModel):
    # Ratio in (0, 1]: 0.99 means 99% availability. Values like 99.0 are rejected.
    availability: float = Field(gt=0, le=1)


class EnvironmentCreate(BaseModel):
    owner: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    resources: ResourceSpec
    slo: SLOSpec
    ttl_hours: int = Field(ge=1)

    @field_validator("owner", "repo", "runtime")
    @classmethod
    def non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class Environment(BaseModel):
    id: str
    owner: str
    repo: str
    runtime: str
    resources: ResourceSpec
    slo: SLOSpec
    ttl_hours: int
    desired_state: DesiredState = DesiredState.ACTIVE
    status: EnvironmentStatus = EnvironmentStatus.REQUESTED
    created_at: datetime
    expires_at: datetime
    last_error: str | None = None


class EnvironmentEvent(BaseModel):
    environment_id: str
    from_status: EnvironmentStatus
    to_status: EnvironmentStatus
    timestamp: datetime
    actor: str
    reason: str
    correlation_id: str
