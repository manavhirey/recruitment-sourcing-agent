from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.jobs.llm import (
    OpenAIResponsesScorecardGateway,
    ScorecardExtractionError,
    extraction_instructions,
)
from app.jobs.schemas import ClientContext, ScorecardDraft
from app.main import create_app

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
    "seniority": ["mid_level"],
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


def test_unknown_seniority_retries_with_its_validation_error(client_context) -> None:
    invalid = {**VALID_DRAFT, "seniority": ["manager"]}
    client = FakeOpenAI([invalid, VALID_DRAFT])
    gateway = OpenAIResponsesScorecardGateway(client, "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert result.seniority == ["mid_level"]
    assert "unknown seniority value: manager" in str(client.responses.calls[1]["input"])


def test_inferred_numeric_bound_requires_confirmation(client_context) -> None:
    draft = {
        **VALID_DRAFT,
        "minimum_years": 5,
        "maximum_years": None,
        "uncertainties": ["Confirm inferred minimum years: 5"],
    }
    gateway = OpenAIResponsesScorecardGateway(FakeOpenAI([draft]), "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert result.unresolved_inferred_items() == result.inferred_item_ids()


def test_generated_draft_cannot_self_confirm_numeric_bound(client_context) -> None:
    confirmation_id = (
        "uncertainty:WyJ1bmNlcnRhaW50eSIsMCwiQ29uZmlybSBpbmZlcnJlZCBtaW5pbXVt"
        "IHllYXJzOiA1Il0"
    )
    draft = {
        **VALID_DRAFT,
        "minimum_years": 5,
        "maximum_years": None,
        "uncertainties": ["Confirm inferred minimum years: 5"],
        "confirmed_inferred_items": [confirmation_id],
    }
    gateway = OpenAIResponsesScorecardGateway(FakeOpenAI([draft]), "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert result.confirmed_inferred_items == []
    assert confirmation_id in result.unresolved_inferred_items()


@pytest.mark.parametrize(
    ("minimum_years", "maximum_years", "uncertainties", "expected"),
    [
        (5, None, [], ["Confirm inferred minimum years: 5"]),
        (None, 8, [], ["Confirm inferred maximum years: 8"]),
        (
            5,
            12,
            ["Confirm inferred minimum years: 5", "Confirm scope with recruiter"],
            [
                "Confirm inferred minimum years: 5",
                "Confirm scope with recruiter",
                "Confirm inferred maximum years: 12",
            ],
        ),
    ],
)
def test_generated_numeric_bounds_fail_closed_with_exact_uncertainties(
    client_context,
    minimum_years: int | None,
    maximum_years: int | None,
    uncertainties: list[str],
    expected: list[str],
) -> None:
    draft = {
        **VALID_DRAFT,
        "minimum_years": minimum_years,
        "maximum_years": maximum_years,
        "uncertainties": uncertainties,
    }
    gateway = OpenAIResponsesScorecardGateway(FakeOpenAI([draft]), "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert result.uncertainties == expected


def test_extraction_instructions_constrain_seniority_and_numeric_inference() -> None:
    instructions = extraction_instructions()

    assert "Use only early_career, mid_level, or senior for seniority." in instructions
    assert "numeric bounds override seniority presets" in instructions


def test_forced_confirmations_truncate_model_uncertainties_to_cap(
    client_context,
) -> None:
    draft = {
        **VALID_DRAFT,
        "uncertainties": [f"Uncertainty {index}" for index in range(20)],
    }
    gateway = OpenAIResponsesScorecardGateway(FakeOpenAI([draft]), "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert len(result.uncertainties) == 20
    assert result.uncertainties[:18] == [f"Uncertainty {index}" for index in range(18)]
    assert "Confirm inferred minimum years: 5" in result.uncertainties
    assert "Confirm inferred maximum years: 12" in result.uncertainties


def test_truncation_preserves_forced_confirmation_present_in_model_list(
    client_context,
) -> None:
    draft = {
        **VALID_DRAFT,
        "maximum_years": None,
        "uncertainties": [f"Uncertainty {index}" for index in range(19)]
        + ["Confirm inferred minimum years: 5"],
    }
    gateway = OpenAIResponsesScorecardGateway(FakeOpenAI([draft]), "gpt-5-mini")

    result = gateway.extract(
        "Hire a product manager with payments experience.", client_context
    )

    assert len(result.uncertainties) == 20
    assert result.uncertainties[-1] == "Confirm inferred minimum years: 5"


def test_create_app_wires_openai_client_with_configured_timeout() -> None:
    settings = Settings.for_test().model_copy(
        update={"scorecard_llm_timeout_seconds": 7}
    )

    with patch("app.main.OpenAI") as openai_factory:
        create_app(settings)

    openai_factory.assert_called_once_with(
        api_key="test-openai-key",
        timeout=7,
        max_retries=1,
    )


def test_two_invalid_extractions_raise_typed_error(client_context) -> None:
    client = FakeOpenAI([{"target_titles": []}, {"criteria": []}])
    gateway = OpenAIResponsesScorecardGateway(client, "gpt-5-mini")

    with pytest.raises(ScorecardExtractionError):
        gateway.extract("Hire a product manager.", client_context)

    assert len(client.responses.calls) == 2
