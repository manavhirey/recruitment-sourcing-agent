from uuid import UUID

from pydantic import BaseModel, Field


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    industry_codes: set[str] = Field(default_factory=set)


class ClientIndustriesUpdate(BaseModel):
    industry_codes: set[str]


class ClientAdjacencyUpdate(BaseModel):
    industry_code: str
    adjacent_industry_code: str


class ClientGrantCreate(BaseModel):
    membership_id: UUID


class ClientResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    industry_codes: list[str]
    adjacent_industries: list[tuple[str, str]]


class ClientGrantResponse(BaseModel):
    client_id: UUID
    membership_id: UUID
