from app.clients.taxonomy import IndustryTaxonomy


def test_fintech_can_be_approved_as_adjacent_to_banking() -> None:
    taxonomy = IndustryTaxonomy.load_version("v1")

    assert taxonomy.contains("financial_services.banking")
    assert taxonomy.contains("technology.fintech")
    assert taxonomy.default_adjacency("financial_services.banking") == {
        "technology.fintech"
    }
