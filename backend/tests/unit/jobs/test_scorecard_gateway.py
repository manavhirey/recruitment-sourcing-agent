from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.jobs.llm import OpenAIResponsesScorecardGateway, ScorecardExtractionError
from app.jobs.schemas import ClientContext, ScorecardDraft

VALID_DRAFT = {
    "target_titles": ["Product Manager"],
    "criteria": [
        {
            "key": "payments",
            "label": "Payments experience",
            "kind": "must_have",
            "source_text": "payments experience",
        }
    ],
    "seniority": ["manager"],
    "minimum_years": 5,
    "maximum_years": 12,
    "locations": ["India"],
    "industry_code": "technology.fintech",
    "suggested_adjacent_industries": ["financial_services.banking"],
    "uncertainties": [],
}


class FakeResponses:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.outputs.pop(0))


class FakeOpenAI:
    def __init__(self, outputs: list[object]) -> None:
        self.responses = FakeResponses(outputs)


@pytest.fixture
def client_context() -> ClientContext:
    return ClientContext(
        client_id=uuid4(),
        industry_codes=("technology.fintech",),
        approved_adjacent_industries=("financial_services.banking",),
    )


def test_valid_extraction_uses_typed_scorecard_schema(client_context) -> None:
    client = FakeOpenAI([VALID_DRAFT])
    gateway = OpenAIResponsesScorecardGateway(client, "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert result.target_titles == ["Product Manager"]
    assert len(client.responses.calls) == 1
    assert client.responses.calls[0]["text_format"] is ScorecardDraft


def test_invalid_extraction_retries_once_with_validation_error(client_context) -> None:
    client = FakeOpenAI([{"target_titles": []}, VALID_DRAFT])
    gateway = OpenAIResponsesScorecardGateway(client, "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert result.industry_code == "technology.fintech"
    assert len(client.responses.calls) == 2
    assert "Validation errors from the prior attempt" in str(
        client.responses.calls[1]["input"]
    )


def test_two_invalid_extractions_raise_typed_error(client_context) -> None:
    client = FakeOpenAI([{"target_titles": []}, {"criteria": []}])
    gateway = OpenAIResponsesScorecardGateway(client, "gpt-5-mini")

    with pytest.raises(ScorecardExtractionError):
        gateway.extract("Hire a product manager.", client_context)

    assert len(client.responses.calls) == 2
