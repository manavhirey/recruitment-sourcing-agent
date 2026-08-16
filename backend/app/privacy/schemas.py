from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PrivacyRequestType(StrEnum):
    ACCESS = "Access"
    CORRECTION = "Correction"
    DELETION = "Deletion"
    OPT_OUT = "Opt Out"


class PrivacyRequestState(StrEnum):
    RECEIVED = "Received"
    IDENTITY_VERIFICATION_REQUIRED = "Identity Verification Required"
    APPROVED = "Approved"
    EXECUTING = "Executing"
    MANUAL_FULFILLMENT_REQUIRED = "Manual Fulfillment Required"
    COMPLETED = "Completed"
    REJECTED = "Rejected"


class PrivacyRequestCreate(BaseModel):
    candidate_id: UUID
    request_type: PrivacyRequestType


class PrivacyRequestReject(BaseModel):
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")


class PrivacyRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    candidate_id: UUID
    request_type: PrivacyRequestType
    state: PrivacyRequestState
    identity_verified_at: datetime | None
    approved_at: datetime | None
    completed_at: datetime | None
    rejected_at: datetime | None
    rejection_reason_code: str | None
    created_at: datetime
    updated_at: datetime
