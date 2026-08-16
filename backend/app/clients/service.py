from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.models import (
    ClientAdjacentIndustry,
    ClientCompany,
    ClientGrant,
    ClientIndustry,
)
from app.clients.taxonomy import IndustryTaxonomy
from app.core.errors import AppError
from app.identity.models import IdentityIdempotencyKey, Membership
from app.identity.schemas import RequestContext, Role
from app.identity.service import IdentityError, MembershipService


class ClientError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ClientGrantResult:
    client_id: UUID
    membership_id: UUID

    def to_payload(self) -> dict[str, str]:
        return {
            "client_id": str(self.client_id),
            "membership_id": str(self.membership_id),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, str]) -> "ClientGrantResult":
        return cls(
            client_id=UUID(payload["client_id"]),
            membership_id=UUID(payload["membership_id"]),
        )


class ClientService:
    def __init__(self, session: Session, hmac_key: bytes) -> None:
        self.session = session
        self.taxonomy = IndustryTaxonomy.load_version("v1")
        # The identity service owns the shared persistent idempotency ledger.
        self._idempotency = MembershipService(session, hmac_key)

    def list_authorized(self, context: RequestContext) -> list[ClientCompany]:
        statement = select(ClientCompany).where(
            ClientCompany.tenant_id == context.tenant_id
        )
        if context.role is Role.RECRUITER:
            allowed_client_ids = context.allowed_client_ids or frozenset()
            statement = statement.where(ClientCompany.id.in_(allowed_client_ids))
        return list(self.session.scalars(statement.order_by(ClientCompany.name)))

    def get_authorized(self, context: RequestContext, client_id: UUID) -> ClientCompany:
        statement = select(ClientCompany).where(
            ClientCompany.id == client_id,
            ClientCompany.tenant_id == context.tenant_id,
        )
        if context.role is Role.RECRUITER:
            allowed_client_ids = context.allowed_client_ids or frozenset()
            statement = statement.where(ClientCompany.id.in_(allowed_client_ids))
        client = self.session.scalar(statement)
        if client is None:
            raise ClientError("client_not_found")
        return client

    def create(
        self,
        context: RequestContext,
        *,
        name: str,
        industry_codes: set[str],
        idempotency_key: str,
    ) -> ClientCompany:
        normalized_name = name.strip().casefold()
        if not normalized_name:
            raise ClientError("client_name_invalid")
        self._validate_industries(industry_codes)
        record = self._begin(
            context,
            "create_client",
            idempotency_key,
            {"name": normalized_name, "industry_codes": sorted(industry_codes)},
        )
        if record.response_payload is not None:
            return self.get_authorized(
                context, UUID(record.response_payload["client_id"])
            )
        client = ClientCompany(
            id=uuid4(),
            tenant_id=context.tenant_id,
            name=name.strip(),
            normalized_name=normalized_name,
        )
        self.session.add(client)
        self.session.flush()
        self._replace_industries(client, industry_codes)
        self._complete(record, {"client_id": str(client.id)})
        return client

    def update_industries(
        self,
        context: RequestContext,
        client_id: UUID,
        industry_codes: set[str],
        idempotency_key: str,
    ) -> ClientCompany:
        self._validate_industries(industry_codes)
        client = self.get_authorized(context, client_id)
        record = self._begin(
            context,
            f"update_client_industries:{client_id}",
            idempotency_key,
            {"industry_codes": sorted(industry_codes)},
        )
        if record.response_payload is not None:
            return self.get_authorized(context, client_id)
        self._replace_industries(client, industry_codes)
        self._complete(record, {"client_id": str(client_id)})
        return client

    def approve_adjacency(
        self,
        context: RequestContext,
        client_id: UUID,
        industry_code: str,
        adjacent_industry_code: str,
        idempotency_key: str,
    ) -> ClientCompany:
        client = self.get_authorized(context, client_id)
        if not self.taxonomy.contains(industry_code) or not self.taxonomy.contains(
            adjacent_industry_code
        ):
            raise ClientError("industry_code_invalid")
        industry_assigned = self.session.scalar(
            select(ClientIndustry.id).where(
                ClientIndustry.client_id == client_id,
                ClientIndustry.industry_code == industry_code,
            )
        )
        if industry_assigned is None:
            raise ClientError("client_industry_not_assigned")
        if not self.taxonomy.is_adjacent(industry_code, adjacent_industry_code):
            raise ClientError("industry_adjacency_invalid")
        record = self._begin(
            context,
            f"approve_client_adjacency:{client_id}",
            idempotency_key,
            {
                "industry_code": industry_code,
                "adjacent_industry_code": adjacent_industry_code,
            },
        )
        if record.response_payload is not None:
            return client
        existing = self.session.scalar(
            select(ClientAdjacentIndustry).where(
                ClientAdjacentIndustry.client_id == client_id,
                ClientAdjacentIndustry.industry_code == industry_code,
                ClientAdjacentIndustry.adjacent_industry_code == adjacent_industry_code,
            )
        )
        if existing is None:
            self.session.add(
                ClientAdjacentIndustry(
                    tenant_id=context.tenant_id,
                    client_id=client_id,
                    industry_code=industry_code,
                    adjacent_industry_code=adjacent_industry_code,
                    approved_by_user_id=context.user_id,
                )
            )
            self.session.flush()
        self._complete(record, {"client_id": str(client_id)})
        return client

    def grant_access(
        self,
        context: RequestContext,
        client_id: UUID,
        membership_id: UUID,
        idempotency_key: str,
    ) -> ClientGrantResult:
        self.get_authorized(context, client_id)
        membership = self.session.scalar(
            select(Membership)
            .where(
                Membership.id == membership_id,
                Membership.tenant_id == context.tenant_id,
                Membership.role == Role.RECRUITER,
                Membership.active.is_(True),
            )
            .with_for_update()
        )
        if membership is None:
            raise ClientError("recruiter_not_found")
        record = self._begin(
            context,
            f"grant_client_access:{client_id}",
            idempotency_key,
            {"membership_id": str(membership_id)},
        )
        if record.response_payload is not None:
            return ClientGrantResult.from_payload(record.response_payload)
        grant = self.session.scalar(
            select(ClientGrant).where(
                ClientGrant.client_id == client_id,
                ClientGrant.membership_id == membership_id,
            )
        )
        if grant is None:
            self.session.add(
                ClientGrant(
                    tenant_id=context.tenant_id,
                    client_id=client_id,
                    membership_id=membership_id,
                    granted_by_user_id=context.user_id,
                )
            )
        allowed_client_ids = set(membership.allowed_client_ids or [])
        allowed_client_ids.add(str(client_id))
        membership.allowed_client_ids = sorted(allowed_client_ids)
        self.session.flush()
        result = ClientGrantResult(client_id=client_id, membership_id=membership_id)
        self._complete(record, result.to_payload())
        return result

    def industries_for(self, client: ClientCompany) -> list[str]:
        return sorted(
            self.session.scalars(
                select(ClientIndustry.industry_code).where(
                    ClientIndustry.client_id == client.id
                )
            )
        )

    def adjacencies_for(self, client: ClientCompany) -> list[tuple[str, str]]:
        rows = self.session.execute(
            select(
                ClientAdjacentIndustry.industry_code,
                ClientAdjacentIndustry.adjacent_industry_code,
            )
            .where(ClientAdjacentIndustry.client_id == client.id)
            .order_by(
                ClientAdjacentIndustry.industry_code,
                ClientAdjacentIndustry.adjacent_industry_code,
            )
        ).all()
        return [(str(row[0]), str(row[1])) for row in rows]

    def _replace_industries(
        self, client: ClientCompany, industry_codes: set[str]
    ) -> None:
        self.session.query(ClientIndustry).filter(
            ClientIndustry.client_id == client.id
        ).delete(synchronize_session=False)
        self.session.add_all(
            ClientIndustry(
                tenant_id=client.tenant_id,
                client_id=client.id,
                industry_code=industry_code,
                taxonomy_version=self.taxonomy.version,
            )
            for industry_code in industry_codes
        )
        self.session.flush()

    def _validate_industries(self, industry_codes: set[str]) -> None:
        if not all(self.taxonomy.contains(code) for code in industry_codes):
            raise ClientError("industry_code_invalid")

    def _begin(
        self,
        context: RequestContext,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> IdentityIdempotencyKey:
        try:
            return self._idempotency.begin_idempotent_mutation(
                tenant_id=context.tenant_id,
                actor_key=str(context.user_id),
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
        except IdentityError as error:
            raise ClientError(error.code) from error

    def _complete(
        self, record: IdentityIdempotencyKey, response_payload: dict[str, str]
    ) -> None:
        self._idempotency.complete_idempotent_mutation(record, response_payload)
