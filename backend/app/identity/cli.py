import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.identity.schemas import IdentityClaims
from app.identity.service import TenantService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision a tenant owner")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    return parser


def provision_tenant(
    settings: Settings,
    *,
    slug: str,
    owner_claims: IdentityClaims,
) -> str:
    if settings.migration_database_url is None:
        raise RuntimeError("MIGRATION_DATABASE_URL is required for tenant provisioning")
    engine = create_engine(settings.migration_database_url)
    try:
        with Session(engine) as session, session.begin():
            tenant = TenantService(session).provision(slug, owner_claims)
            tenant_id = str(tenant.id)
        return tenant_id
    finally:
        engine.dispose()


def main() -> None:
    arguments = build_parser().parse_args()
    tenant_id = provision_tenant(
        get_settings(),
        slug=arguments.slug,
        owner_claims=IdentityClaims(
            subject=arguments.subject,
            email=arguments.email,
            name=arguments.name,
            email_verified=True,
        ),
    )
    print(tenant_id)


if __name__ == "__main__":
    main()
