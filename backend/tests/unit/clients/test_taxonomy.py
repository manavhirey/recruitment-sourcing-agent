from datetime import UTC
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.models import ClientCompany, ClientIndustry
from app.clients.taxonomy import IndustryTaxonomy
from app.core.database import Base


def test_fintech_can_be_approved_as_adjacent_to_banking() -> None:
    taxonomy = IndustryTaxonomy.load_version("v1")

    assert taxonomy.contains("financial_services.banking")
    assert taxonomy.contains("technology.fintech")
    assert taxonomy.default_adjacency("financial_services.banking") == {
        "technology.fintech"
    }


def test_client_industry_persists_a_utc_created_timestamp() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    client = ClientCompany(
        id=uuid4(),
        tenant_id=uuid4(),
        name="Timestamp Client",
        normalized_name="timestamp client",
    )
    industry = ClientIndustry(
        id=uuid4(),
        tenant_id=client.tenant_id,
        client_id=client.id,
        industry_code="technology.fintech",
        taxonomy_version="v1",
    )

    with Session(engine) as session:
        session.add_all((client, industry))
        session.flush()

        assert industry.created_at.tzinfo is UTC
