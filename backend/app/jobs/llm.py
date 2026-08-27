import json
from typing import Any, Protocol, cast

from pydantic import ValidationError

from app.core.errors import AppError
from app.jobs.schemas import ClientContext, ScorecardDraft


class ScorecardExtractionError(AppError):
    code = "scorecard_extraction_failed"

    def __init__(self, validation_errors: str) -> None:
        self.validation_errors = validation_errors
        super().__init__(self.code)


class ScorecardGateway(Protocol):
    def extract(
        self, job_description: str, client_context: ClientContext
    ) -> ScorecardDraft: ...


class _ParsedResponse(Protocol):
    output_parsed: object


class _ResponsesAPI(Protocol):
    def parse(self, **kwargs: Any) -> _ParsedResponse: ...


class _OpenAIClient(Protocol):
    responses: _ResponsesAPI


def extraction_instructions() -> str:
    return (
        "Extract only job-relevant criteria. Mark every inference. "
        "Never infer protected characteristics or work authorization. "
        "Use only early_career, mid_level, or senior for seniority. "
        "Put explicit numeric experience requirements in minimum_years and maximum_years; "
        "numeric bounds override seniority presets. If a numeric bound is inferred rather "
        "than stated, add the exact uncertainty 'Confirm inferred minimum years: N' or "
        "'Confirm inferred maximum years: N' so recruiter confirmation is required. "
        "Return data that validates against the supplied scorecard schema."
    )


class OpenAIResponsesScorecardGateway:
    def __init__(self, client: object, model: str) -> None:
        self._client = cast(_OpenAIClient, client)
        self._model = model

    def extract(
        self, job_description: str, client_context: ClientContext
    ) -> ScorecardDraft:
        extraction_input = json.dumps(
            {
                "job_description": job_description,
                "client_context": client_context.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        validation_errors = ""
        for attempt in range(2):
            request_input = extraction_input
            if validation_errors:
                request_input += (
                    f"\nValidation errors from the prior attempt:\n{validation_errors}"
                )
            try:
                response = self._client.responses.parse(
                    model=self._model,
                    instructions=extraction_instructions(),
                    input=request_input,
                    text_format=ScorecardDraft,
                )
                draft = ScorecardDraft.model_validate(response.output_parsed)
                return _require_numeric_bound_confirmation(draft)
            except ValidationError as error:
                validation_errors = error.json(include_url=False)
                if attempt == 1:
                    raise ScorecardExtractionError(validation_errors) from error
        raise AssertionError("scorecard extraction retry loop did not terminate")


def _require_numeric_bound_confirmation(draft: ScorecardDraft) -> ScorecardDraft:
    values = draft.model_dump()
    uncertainties = list(draft.uncertainties)
    for label, bound in (
        ("minimum", draft.minimum_years),
        ("maximum", draft.maximum_years),
    ):
        if bound is None:
            continue
        uncertainty = f"Confirm inferred {label} years: {bound}"
        if uncertainty not in uncertainties:
            uncertainties.append(uncertainty)
    values["uncertainties"] = uncertainties
    return ScorecardDraft.model_validate(values)
