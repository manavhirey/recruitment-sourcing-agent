from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from email_validator import validate_email
from pydantic import AliasChoices, BaseModel, EmailStr, Field, PlainValidator


def _validate_oidc_email(value: Any) -> str:
    return validate_email(str(value), test_environment=True).normalized


OIDCEmail = Annotated[
    EmailStr,
    PlainValidator(_validate_oidc_email, json_schema_input_type=str),
]


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    RECRUITER = "recruiter"


class IdentityClaims(BaseModel):
    subject: str = Field(validation_alias=AliasChoices("subject", "sub"))
    email: OIDCEmail
    name: str
    email_verified: bool = False


class RequestContext(BaseModel):
    tenant_id: UUID
    user_id: UUID
    role: Role
    allowed_client_ids: frozenset[UUID] | None = None


class MeResponse(RequestContext):
    display_name: str
    email: OIDCEmail


class MemberResponse(BaseModel):
    membership_id: UUID
    user_id: UUID
    email: OIDCEmail
    display_name: str
    role: Role
    allowed_client_ids: frozenset[UUID] | None
    active: bool


class InvitationCreate(BaseModel):
    email: OIDCEmail
    role: Role


class InvitationResponse(BaseModel):
    invitation_id: UUID
    token: str
    expires_at: datetime


class RoleUpdate(BaseModel):
    role: Role


class MembershipResponse(BaseModel):
    membership_id: UUID
    tenant_id: UUID
    user_id: UUID
    role: Role
    active: bool
