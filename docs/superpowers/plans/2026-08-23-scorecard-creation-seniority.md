# Scorecard Creation and Seniority Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let recruiters populate an editable job description from a safe PDF or DOCX upload and use canonical, numeric-evidence-based seniority requirements when generating and matching a scorecard.

**Architecture:** Add a request-scoped document extraction boundary to the existing jobs domain and proxy it through a bounded multipart BFF route into the current intake form. Add a canonical seniority policy used by scorecard validation, matching, and provider query planning while leaving historical confirmed scorecards readable and existing candidate results unchanged.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, pypdf, python-docx, python-multipart, SQLAlchemy, pytest/Hypothesis, Next.js 16, React 19, TypeScript 5.9, Zod 4, Vitest/Testing Library/MSW, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-23-scorecard-creation-seniority-design.md`

## Global Constraints

- Continue supporting pasted job-description text.
- Accept exactly one `.pdf` or `.docx` file containing at most 10,000,000 bytes.
- Keep the existing 50,000-Unicode-code-point job-description limit.
- Do not perform OCR, retain uploads, persist filenames/media types, or pass file bytes to the language model; discard the request-scoped upload on every success and failure path.
- Reject corrupted and encrypted documents with code `job_description_file_unreadable` and the exact message “The uploaded job description file is corrupted or might be password-protected.”
- Enforce 200 PDF pages, 2,000 DOCX archive entries, 50,000,000 expanded DOCX bytes, no nested archives, and a 10-second request deadline.
- Canonical seniority values are `early_career`, `mid_level`, and `senior`.
- Preset ranges are inclusive: Early-Career `[0, 3]`, Mid-Level `[3, 9]`, and Senior `[10, infinity]`.
- Multiple presets form a union; they must never be collapsed into one envelope.
- At least one non-null custom bound activates a single custom interval and overrides all presets.
- Zero presets and no custom bounds is valid and produces no seniority-range criterion.
- Numeric `candidate.years_experience` is the only evidence for seniority-range matching; missing numeric experience is `unknown`.
- New matching results use `matching-v2`; historical scores, evidence, and model versions remain unchanged.
- Use TDD for every task and commit only after its focused tests pass.
- Do not stage or modify the user's untracked `.agents/` or `docs/feature-requests/` content.

## Reference Documentation

- FastAPI file uploads and `UploadFile`: https://fastapi.tiangolo.com/tutorial/request-files/
- pypdf `PdfReader` and `is_encrypted`: https://pypdf.readthedocs.io/en/5.0.1/modules/PdfReader.html
- python-docx document API: https://python-docx.readthedocs.io/en/latest/user/api-concepts.html
- python-docx table traversal: https://python-docx.readthedocs.io/en/latest/user/tables.html

## File and Responsibility Map

### Backend domain and API

- Create `backend/app/jobs/seniority.py`: canonical enum, preset metadata, interval resolution, and legacy-value validation.
- Modify `backend/app/jobs/schemas.py`: canonical draft types, historical confirmed-scorecard compatibility, and serialized seniority options.
- Modify `backend/app/jobs/service.py`: attach seniority options to draft responses without changing persistence.
- Modify `backend/app/jobs/llm.py`: constrain model output to canonical values and explicit/inferred numeric-bound rules.
- Modify `backend/app/matching/engine.py`: evaluate effective numeric intervals only.
- Modify `backend/app/matching/schemas.py`: set `matching-v2` for new results.
- Modify `backend/app/providers/query_planner.py`: map active presets to recall-oriented Apollo filters and omit stale presets under a custom override.
- Modify `backend/app/sourcing/service.py`: reject starting a new run from an unrecognized historical seniority value.
- Create `backend/app/jobs/document_extraction.py`: signature validation, safe PDF/DOCX parsing, normalization, and typed failures.
- Create `backend/app/jobs/document_router.py`: authenticated multipart endpoint, bounded reading, deadline, cleanup, and stable HTTP errors.
- Modify `backend/app/main.py`: inject the document extractor and register the new router.
- Modify `backend/pyproject.toml` and `backend/uv.lock`: locked multipart and parser dependencies.

### Web boundary and UI

- Create `web/lib/document-extraction-bff.ts`: same-origin, tenant, idempotency, multipart, file-count, and size boundary.
- Create `web/app/api/bff/job-descriptions/extract/route.ts`: route adapter to the upload BFF helper.
- Create `web/components/jobs/JobDescriptionUpload.tsx`: file chooser, replacement confirmation, progress, errors, and extraction callback.
- Modify `web/components/jobs/JobIntakeForm.tsx`: connect extraction to React Hook Form and disable generation while busy.
- Modify `web/components/scorecards/ScorecardEditor.tsx`: preset checkboxes and explicit custom override.
- Modify `web/lib/request-schemas.ts`: canonical seniority request validation.
- Modify `web/lib/schemas.ts` and regenerate `web/lib/generated-api.ts`: expose document extraction and seniority-option response types.

### Tests

- Create `backend/tests/unit/jobs/test_seniority.py`.
- Modify `backend/tests/unit/jobs/test_scorecard_schema.py` and `backend/tests/unit/jobs/test_scorecard_gateway.py`.
- Modify `backend/tests/unit/matching/test_scoring.py` and `backend/tests/property/test_matching_determinism.py`.
- Modify `backend/tests/unit/providers/test_query_planner.py` and sourcing integration tests.
- Create `backend/tests/unit/jobs/test_document_extraction.py`.
- Create `backend/tests/job_description_fixtures.py` for deterministic in-memory PDF and DOCX builders shared by unit and API tests.
- Create `backend/tests/integration/jobs/test_document_extraction_api.py`.
- Create `web/tests/security/document-extraction-bff.test.ts`.
- Create `web/tests/jobs/job-description-upload.test.tsx` and modify `web/tests/jobs/job-intake.test.tsx`.
- Modify `web/tests/scorecards/scorecard-editor.test.tsx`, `web/tests/fixtures.ts`, and affected preview fixtures.
- Modify `web/e2e/task-13-review.spec.ts` for upload and canonical-seniority coverage.

---

### Task 1: Canonical Seniority Domain Contract

**Files:**
- Create: `backend/app/jobs/seniority.py`
- Create: `backend/tests/unit/jobs/test_seniority.py`
- Modify: `backend/app/jobs/schemas.py:77-164`
- Modify: `backend/app/jobs/schemas.py:233-256`
- Modify: `backend/app/jobs/service.py:522-534`
- Modify: `backend/app/jobs/llm.py:31-41`
- Modify: `backend/tests/unit/jobs/test_scorecard_schema.py`
- Modify: `backend/tests/unit/jobs/test_scorecard_gateway.py`

**Interfaces:**
- Produces: `SeniorityLevel`, `ExperienceInterval`, `SeniorityPreset`, `SENIORITY_PRESETS`, `normalize_draft_seniority(values)`, `validate_confirmed_seniority(values)`, and `effective_experience_intervals(seniority, minimum_years, maximum_years)`.
- Produces: `ScorecardDraftResponse.seniority_options: tuple[SeniorityOption, ...]` for the scorecard editor.
- Consumes: existing `minimum_years` and `maximum_years` columns; no database migration.

- [ ] **Step 1: Write failing seniority policy tests**

```python
# backend/tests/unit/jobs/test_seniority.py
import pytest

from app.jobs.seniority import (
    SeniorityLevel,
    effective_experience_intervals,
    normalize_draft_seniority,
)


@pytest.mark.parametrize(
    ("years", "early", "mid", "senior"),
    [
        (0.0, True, False, False),
        (3.0, True, True, False),
        (4.0, False, True, False),
        (9.0, False, True, False),
        (10.0, False, False, True),
    ],
)
def test_preset_boundaries_are_inclusive(years, early, mid, senior):
    levels = [
        SeniorityLevel.EARLY_CAREER,
        SeniorityLevel.MID_LEVEL,
        SeniorityLevel.SENIOR,
    ]
    actual = [
        any(interval.contains(years) for interval in effective_experience_intervals([level], None, None))
        for level in levels
    ]
    assert actual == [early, mid, senior]


def test_disjoint_presets_remain_disjoint():
    intervals = effective_experience_intervals(
        [SeniorityLevel.EARLY_CAREER, SeniorityLevel.SENIOR], None, None
    )
    assert any(interval.contains(3.0) for interval in intervals)
    assert not any(interval.contains(7.0) for interval in intervals)
    assert any(interval.contains(12.0) for interval in intervals)


def test_open_custom_range_overrides_all_presets():
    intervals = effective_experience_intervals(
        [SeniorityLevel.EARLY_CAREER], 5, None
    )
    assert not any(interval.contains(3.0) for interval in intervals)
    assert any(interval.contains(5.0) for interval in intervals)


def test_draft_aliases_normalize_and_unknown_values_fail():
    assert normalize_draft_seniority(["mid-level", "SENIOR", "senior"]) == (
        SeniorityLevel.MID_LEVEL,
        SeniorityLevel.SENIOR,
    )
    with pytest.raises(ValueError, match="unknown seniority value"):
        normalize_draft_seniority(["manager"])
```

- [ ] **Step 2: Run the policy tests and confirm the missing module failure**

Run: `cd backend && uv run pytest tests/unit/jobs/test_seniority.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'app.jobs.seniority'`.

- [ ] **Step 3: Implement the canonical policy**

```python
# backend/app/jobs/seniority.py
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class SeniorityLevel(StrEnum):
    EARLY_CAREER = "early_career"
    MID_LEVEL = "mid_level"
    SENIOR = "senior"


@dataclass(frozen=True)
class ExperienceInterval:
    minimum_years: int
    maximum_years: int | None

    def contains(self, years: float) -> bool:
        return years >= self.minimum_years and (
            self.maximum_years is None or years <= self.maximum_years
        )


@dataclass(frozen=True)
class SeniorityPreset:
    value: SeniorityLevel
    label: str
    minimum_years: int
    maximum_years: int | None


SENIORITY_PRESETS = (
    SeniorityPreset(SeniorityLevel.EARLY_CAREER, "Early-Career", 0, 3),
    SeniorityPreset(SeniorityLevel.MID_LEVEL, "Mid-Level", 3, 9),
    SeniorityPreset(SeniorityLevel.SENIOR, "Senior", 10, None),
)
_PRESET_BY_LEVEL = {preset.value: preset for preset in SENIORITY_PRESETS}
_ALIASES = {
    "early_career": SeniorityLevel.EARLY_CAREER,
    "early-career": SeniorityLevel.EARLY_CAREER,
    "mid_level": SeniorityLevel.MID_LEVEL,
    "mid-level": SeniorityLevel.MID_LEVEL,
    "senior": SeniorityLevel.SENIOR,
}


def normalize_draft_seniority(values: Iterable[str]) -> tuple[SeniorityLevel, ...]:
    requested: set[SeniorityLevel] = set()
    for raw in values:
        value = _ALIASES.get(raw.strip().casefold())
        if value is None:
            raise ValueError(f"unknown seniority value: {raw}")
        requested.add(value)
    return tuple(preset.value for preset in SENIORITY_PRESETS if preset.value in requested)


def validate_confirmed_seniority(values: Iterable[str]) -> tuple[SeniorityLevel, ...]:
    return normalize_draft_seniority(values)


def effective_experience_intervals(
    seniority: Sequence[SeniorityLevel | str],
    minimum_years: int | None,
    maximum_years: int | None,
) -> tuple[ExperienceInterval, ...]:
    if minimum_years is not None or maximum_years is not None:
        return (ExperienceInterval(minimum_years or 0, maximum_years),)
    levels = normalize_draft_seniority(str(value) for value in seniority)
    return tuple(
        ExperienceInterval(
            _PRESET_BY_LEVEL[level].minimum_years,
            _PRESET_BY_LEVEL[level].maximum_years,
        )
        for level in levels
    )
```

- [ ] **Step 4: Refactor scorecard schemas without breaking historical reads**

Factor the shared fields into `ScorecardContent`. Keep `ScorecardContent.seniority` as `list[str]` so a confirmed historical record such as `manager` can still be serialized. Override only `ScorecardDraft.seniority` as `list[SeniorityLevel]`, with a `mode="before"` validator that calls `normalize_draft_seniority`. Keep `EditableScorecardDraft.seniority` and `ConfirmedScorecard.seniority` as `list[str]`: the editable response must carry an unrecognized legacy value to the UI so the recruiter can remove it. Keep `ConfirmedScorecard.to_draft()` as the explicit point where unknown historical data fails canonical validation.

```python
class SeniorityOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: SeniorityLevel
    label: str
    minimum_years: int
    maximum_years: int | None


class ScorecardDraft(ScorecardContent):
    seniority: list[SeniorityLevel] = Field(max_length=3)

    @field_validator("seniority", mode="before")
    @classmethod
    def canonicalize_seniority(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return list(normalize_draft_seniority(str(item) for item in value))


class ScorecardDraftResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: UUID
    draft_revision: int
    draft: ScorecardDraft | EditableScorecardDraft
    original_job_description: str
    extraction_status: ExtractionStatus
    extraction_warning: str | None
    seniority_options: tuple[SeniorityOption, ...]
```

In `JobService._draft_response`, try `ScorecardDraft.model_validate(job.draft_payload)` first and fall back to `EditableScorecardDraft.model_validate(job.draft_payload)` when a legacy value prevents canonical validation. Build `seniority_options` from `SENIORITY_PRESETS`. Do not add the options to `draft_payload` or any database record. Add a service test that stores `seniority=["manager"]`, reloads the draft successfully as editable data, and receives all three canonical options.

- [ ] **Step 5: Add schema tests for canonical drafts and historical confirmed records**

```python
def _valid_draft(**overrides: object) -> ScorecardDraft:
    values: dict[str, object] = {
        "target_titles": ["Product Designer"],
        "criteria": [
            ScorecardCriterion(
                key="product_design",
                label="Product design experience",
                kind=CriterionKind.MUST_HAVE,
            )
        ],
        "seniority": [],
        "minimum_years": None,
        "maximum_years": None,
        "locations": [],
        "industry_code": "technology.software",
        "suggested_adjacent_industries": [],
        "uncertainties": [],
    }
    values.update(overrides)
    return ScorecardDraft.model_validate(values)


def test_scorecard_draft_normalizes_known_seniority_aliases():
    draft = _valid_draft(seniority=["mid-level", "SENIOR", "senior"])
    assert draft.seniority == [SeniorityLevel.MID_LEVEL, SeniorityLevel.SENIOR]


def test_scorecard_draft_rejects_legacy_unknown_seniority():
    with pytest.raises(ValidationError, match="unknown seniority value"):
        _valid_draft(seniority=["manager"])


def test_confirmed_historical_scorecard_remains_readable_but_not_reusable():
    scorecard = ConfirmedScorecard.model_validate({
        **_valid_draft().model_dump(mode="json"),
        "id": uuid4(),
        "job_id": uuid4(),
        "version": 1,
        "confirmed_at": datetime(2026, 8, 23, tzinfo=UTC),
        "extraction_status": "ready",
        "seniority": ["manager"],
    })
    assert scorecard.seniority == ["manager"]
    with pytest.raises(ValidationError, match="unknown seniority value"):
        scorecard.to_draft()
```

- [ ] **Step 6: Constrain scorecard gateway output and inference instructions**

Add these exact sentences to `extraction_instructions()`:

```python
"Use only early_career, mid_level, or senior for seniority. "
"Put explicit numeric experience requirements in minimum_years and maximum_years; "
"numeric bounds override seniority presets. If a numeric bound is inferred rather "
"than stated, add the exact uncertainty 'Confirm inferred minimum years: N' or "
"'Confirm inferred maximum years: N' so recruiter confirmation is required. "
```

Change `VALID_DRAFT` in `test_scorecard_gateway.py` to canonical values. Add a retry test where the first response contains `seniority=["manager"]` and the second contains `seniority=["mid_level"]`; assert that the validation error is included in the second request. Add a gateway result with `minimum_years=5` and `uncertainties=["Confirm inferred minimum years: 5"]`; assert that its uncertainty confirmation ID is unresolved until included in `confirmed_inferred_items`.

- [ ] **Step 7: Run focused jobs tests**

Run: `cd backend && uv run pytest tests/unit/jobs/test_seniority.py tests/unit/jobs/test_scorecard_schema.py tests/unit/jobs/test_scorecard_gateway.py tests/integration/jobs/test_scorecard_versioning.py -v`

Expected: PASS. Update existing fixtures that represent new drafts from `manager` to `mid_level`; retain at least one explicit historical `manager` fixture.

- [ ] **Step 8: Commit the canonical contract**

```bash
git add backend/app/jobs/seniority.py backend/app/jobs/schemas.py backend/app/jobs/service.py backend/app/jobs/llm.py backend/tests/unit/jobs/test_seniority.py backend/tests/unit/jobs/test_scorecard_schema.py backend/tests/unit/jobs/test_scorecard_gateway.py backend/tests/integration/jobs/test_scorecard_versioning.py
git commit -m "feat: define canonical scorecard seniority policy"
```

### Task 2: Numeric Seniority Matching and `matching-v2`

**Files:**
- Modify: `backend/app/matching/engine.py:159-160`
- Modify: `backend/app/matching/engine.py:296-359`
- Modify: `backend/app/matching/schemas.py:42-52`
- Modify: `backend/tests/unit/matching/test_scoring.py`
- Modify: `backend/tests/property/test_matching_determinism.py`
- Modify: `backend/tests/unit/matching/conftest.py`

**Interfaces:**
- Consumes: `effective_experience_intervals(...) -> tuple[ExperienceInterval, ...]` from Task 1.
- Produces: one `component.years_experience` evaluation based only on numeric years, or no scope evaluation when the scorecard has no requirement.
- Produces: `MatchResult.scoring_version == "matching-v2"` for every new evaluation.

- [ ] **Step 1: Write failing boundary, override, missing-evidence, and empty-requirement tests**

```python
@pytest.mark.parametrize(
    ("seniority", "years", "state"),
    [
        (["early_career"], 3.0, EvidenceState.SUPPORTED),
        (["mid_level"], 3.0, EvidenceState.SUPPORTED),
        (["mid_level"], 10.0, EvidenceState.FAILED),
        (["early_career", "senior"], 7.0, EvidenceState.FAILED),
        (["early_career", "senior"], 12.0, EvidenceState.SUPPORTED),
    ],
)
def test_seniority_presets_match_numeric_years(engine, scorecard_factory, candidate_factory, seniority, years, state):
    result = engine.evaluate(
        scorecard_factory(seniority=seniority, minimum_years=None, maximum_years=None),
        candidate_factory(years_experience=years, seniority="unrelated-provider-label"),
    )
    evaluation = next(item for item in result.criteria if item.key == "component.years_experience")
    assert evaluation.state is state


def test_custom_open_range_overrides_presets(engine, scorecard_factory, candidate_factory):
    result = engine.evaluate(
        scorecard_factory(seniority=["early_career"], minimum_years=5, maximum_years=None),
        candidate_factory(years_experience=6.0),
    )
    assert next(item for item in result.criteria if item.key == "component.years_experience").state is EvidenceState.SUPPORTED


def test_title_or_provider_label_cannot_replace_missing_years(engine, scorecard_factory, candidate_factory):
    result = engine.evaluate(
        scorecard_factory(seniority=["senior"], minimum_years=None, maximum_years=None),
        candidate_factory(years_experience=None, seniority="senior", current_title="Senior Director"),
    )
    evaluation = next(item for item in result.criteria if item.key == "component.years_experience")
    assert evaluation.state is EvidenceState.UNKNOWN
    assert evaluation.points == 0


def test_no_requirement_produces_no_scope_criterion(engine, scorecard_factory, candidate_factory):
    result = engine.evaluate(
        scorecard_factory(seniority=[], minimum_years=None, maximum_years=None),
        candidate_factory(years_experience=None),
    )
    assert not any(item.key == "component.years_experience" for item in result.criteria)
    assert result.breakdown.scope_seniority_years == 0
```

- [ ] **Step 2: Run the focused matching tests and verify old behavior fails**

Run: `cd backend && uv run pytest tests/unit/matching/test_scoring.py -v`

Expected: FAIL because the engine still compares `candidate.seniority`, does not derive preset ranges, and creates an unknown scope atom for an empty requirement.

- [ ] **Step 3: Replace the two-atom seniority logic with one numeric interval evaluation**

```python
scope_atoms = self._scope_evaluations(scorecard, candidate)
scope_score = _allocate_supported_points(scope_atoms, 25) if scope_atoms else 0


def _scope_evaluations(self, scorecard, candidate):
    intervals = effective_experience_intervals(
        scorecard.seniority,
        scorecard.minimum_years,
        scorecard.maximum_years,
    )
    if not intervals:
        return []
    if candidate.years_experience is None:
        return [
            _unknown_atom(
                "component.years_experience",
                "Years of experience",
                "candidate years of experience",
            )
        ]
    return [
        _evidenced_atom(
            key="component.years_experience",
            label="Years of experience",
            supported=any(interval.contains(candidate.years_experience) for interval in intervals),
            evidence=(str(candidate.years_experience),),
            source_refs=("candidate.years_experience",),
            evidence_name="candidate years of experience",
        )
    ]
```

Remove `_canonical_seniority` and its scope-only aliases if no remaining call site uses them. Do not remove title-level helpers used by career-trajectory scoring.

- [ ] **Step 4: Set the new result version and update deterministic properties**

Change the existing `MatchResult.scoring_version` declaration to:

```python
scoring_version: str = "matching-v2"
```

Update the property-test strategies to generate only canonical scorecard values while continuing to generate arbitrary provider `candidate.seniority` labels. Add the invariant that changing only `candidate.seniority` cannot change a result when `years_experience` is fixed.

- [ ] **Step 5: Run matching unit and property tests**

Run: `cd backend && uv run pytest tests/unit/matching tests/property/test_matching_determinism.py -v`

Expected: PASS with all totals equal to their component sum and all new results reporting `matching-v2`.

- [ ] **Step 6: Commit numeric matching**

```bash
git add backend/app/matching/engine.py backend/app/matching/schemas.py backend/tests/unit/matching backend/tests/property/test_matching_determinism.py
git commit -m "feat: match seniority from numeric experience"
```

### Task 3: Provider Query Semantics and Historical Run Guard

**Files:**
- Modify: `backend/app/providers/query_planner.py:1-95`
- Modify: `backend/tests/unit/providers/test_query_planner.py`
- Modify: `backend/app/sourcing/service.py:89-139`
- Modify: `backend/tests/integration/sourcing/test_service.py`
- Modify: `backend/app/sourcing/router.py:145-163`

**Interfaces:**
- Consumes: canonical levels and `validate_confirmed_seniority()` from Task 1.
- Produces: connector seniority filters only when presets are active and no custom bound exists.
- Produces: `SourcingError("scorecard_seniority_revision_required")` before a run is created from unknown historical values.

- [ ] **Step 1: Write failing query-planner tests**

```python
@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("early_career", ("entry", "intern")),
        ("mid_level", ("entry", "senior", "manager")),
        ("senior", ("senior", "manager", "director", "head", "vp", "c_suite")),
    ],
)
def test_query_planner_maps_canonical_presets_for_recall(level, expected):
    queries = QueryPlanner().compile(_scorecard(seniority=[level]))
    assert queries
    assert all(query.seniorities == expected for query in queries)


def test_custom_bounds_omit_inactive_provider_seniority_filters():
    queries = QueryPlanner().compile(
        _scorecard(seniority=["early_career"], minimum_years=5, maximum_years=8)
    )
    assert all(query.seniorities == () for query in queries)


def test_multiple_presets_use_stable_union():
    queries = QueryPlanner().compile(
        _scorecard(seniority=["senior", "early_career"])
    )
    assert all(
        query.seniorities == ("entry", "intern", "senior", "manager", "director", "head", "vp", "c_suite")
        for query in queries
    )
```

- [ ] **Step 2: Run query-planner tests and verify they fail against free-form aliases**

Run: `cd backend && uv run pytest tests/unit/providers/test_query_planner.py -v`

Expected: FAIL because the existing planner has no canonical mapping and still applies presets when custom bounds exist.

- [ ] **Step 3: Implement explicit connector mappings and override suppression**

```python
_APOLLO_BY_LEVEL = {
    SeniorityLevel.EARLY_CAREER: ("entry", "intern"),
    SeniorityLevel.MID_LEVEL: ("entry", "senior", "manager"),
    SeniorityLevel.SENIOR: ("senior", "manager", "director", "head", "vp", "c_suite"),
}


def _provider_seniorities(scorecard: ConfirmedScorecard) -> tuple[str, ...]:
    if scorecard.minimum_years is not None or scorecard.maximum_years is not None:
        return ()
    levels = validate_confirmed_seniority(scorecard.seniority)
    return _stable_unique(
        value for level in levels for value in _APOLLO_BY_LEVEL[level]
    )
```

Replace the old free-form alias comprehension in `QueryPlanner.compile()` with `_provider_seniorities(scorecard)`.

- [ ] **Step 4: Write and implement the historical run guard**

```python
def test_unknown_historical_seniority_requires_revision_before_run(
    service_scenario: dict[str, Any],
) -> None:
    scenario = service_scenario
    scorecard = scenario["session"].scalar(
        select(ScorecardVersion).where(
            ScorecardVersion.id == scenario["confirmed_job"].current_scorecard_id
        )
    )
    assert scorecard is not None
    scorecard.seniority = ["manager"]
    scenario["session"].flush()
    service = SourcingService(scenario["session"], b"test-suppression-key")

    with pytest.raises(SourcingError, match="scorecard_seniority_revision_required"):
        service.start_with_outcome(
            scenario["context"],
            scenario["confirmed_job"].id,
            idempotency_key="legacy-run",
        )
    assert scenario["session"].scalar(
        select(func.count()).select_from(SourcingRun)
    ) == 0
```

Change the `service_scenario` default scorecard from `manager` to `mid_level`. In `SourcingService.start_with_outcome()`, validate `scorecard.seniority` immediately after loading the current version and before checking or creating a run. Translate `ValueError` to `SourcingError("scorecard_seniority_revision_required")`. Map that code to HTTP 409 in the sourcing router.

- [ ] **Step 5: Run provider and sourcing tests**

Run: `cd backend && uv run pytest tests/unit/providers/test_query_planner.py tests/integration/sourcing/test_service.py tests/integration/sourcing/test_router.py -v`

Expected: PASS; no run row is created for unknown historical seniority.

- [ ] **Step 6: Commit provider and run behavior**

```bash
git add backend/app/providers/query_planner.py backend/app/sourcing/service.py backend/app/sourcing/router.py backend/tests/unit/providers/test_query_planner.py backend/tests/integration/sourcing/test_service.py backend/tests/integration/sourcing/test_router.py
git commit -m "feat: apply canonical seniority to sourcing queries"
```

### Task 4: Safe PDF and DOCX Extraction Core

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/app/jobs/document_extraction.py`
- Create: `backend/tests/job_description_fixtures.py`
- Create: `backend/tests/unit/jobs/test_document_extraction.py`

**Interfaces:**
- Produces: `ExtractedJobDescription(text: str, filename: str, media_type: str)`.
- Produces: `DocumentExtractionError(code: str)` with only the stable codes from the spec.
- Produces: `JobDescriptionExtractor` protocol and `DefaultJobDescriptionExtractor.extract(*, data: bytes, filename: str, media_type: str | None)`.

- [ ] **Step 1: Add locked multipart and parser dependencies**

Run from repository root:

```bash
uv add --project backend "python-multipart>=0.0.20" "pypdf>=5.0" "python-docx>=1.2"
```

Expected: `backend/pyproject.toml` and `backend/uv.lock` include the three production dependencies and `uv sync --project backend --frozen` succeeds.

- [ ] **Step 2: Write failing parser tests with in-memory documents**

Create the shared builders in `backend/tests/job_description_fixtures.py`. Use python-docx to generate DOCX bytes and pypdf low-level objects to generate a text PDF without adding a test-only PDF library; import both builders into the unit test:

```python
def readable_docx() -> bytes:
    output = BytesIO()
    document = Document()
    document.add_heading("Senior Product Designer", level=1)
    document.add_paragraph("Lead product design for the growth team.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Experience"
    table.cell(0, 1).text = "10+ years"
    document.save(output)
    return output.getvalue()


def readable_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Senior Product Designer) Tj ET")
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    return output.getvalue()
```

Add tests for readable PDF/DOCX, an encrypted PDF, corrupt bytes, an empty PDF, MIME/extension/signature mismatch, 201 PDF pages, 2,001 ZIP entries, more than 50,000,000 declared expanded bytes, a nested `.zip` entry, no readable text, normalization, and 50,001 extracted characters.

- [ ] **Step 3: Run parser tests and confirm the module is missing**

Run: `cd backend && uv run pytest tests/unit/jobs/test_document_extraction.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'app.jobs.document_extraction'`.

- [ ] **Step 4: Implement the typed extraction boundary and signature checks**

```python
MAX_FILE_BYTES = 10_000_000
MAX_TEXT_LENGTH = 50_000
MAX_PDF_PAGES = 200
MAX_DOCX_ENTRIES = 2_000
MAX_DOCX_EXPANDED_BYTES = 50_000_000


@dataclass(frozen=True)
class ExtractedJobDescription:
    text: str
    filename: str
    media_type: str


class DocumentExtractionError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class JobDescriptionExtractor(Protocol):
    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        pass
```

`DefaultJobDescriptionExtractor.extract()` must reject size before document parsing, require `.pdf` plus `%PDF-` or `.docx` plus ZIP signature and the two required OOXML members, and return the canonical media type rather than echoing an untrusted header. Treat a `.docx` file with the OLE compound-document signature `D0 CF 11 E0 A1 B1 1A E1` as password-protected/unreadable, not as an unsupported type.

- [ ] **Step 5: Implement bounded PDF and DOCX parsing**

```python
def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DocumentExtractionError("job_description_file_unreadable")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentExtractionError("job_description_file_too_complex")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except DocumentExtractionError:
        raise
    except (PdfReadError, OSError, ValueError, KeyError) as error:
        raise DocumentExtractionError("job_description_file_unreadable") from error


def _validate_docx_package(data: bytes) -> None:
    with ZipFile(BytesIO(data)) as package:
        entries = package.infolist()
        if len(entries) > MAX_DOCX_ENTRIES:
            raise DocumentExtractionError("job_description_file_too_complex")
        if sum(entry.file_size for entry in entries) > MAX_DOCX_EXPANDED_BYTES:
            raise DocumentExtractionError("job_description_file_too_complex")
        names = {entry.filename for entry in entries}
        if not {"[Content_Types].xml", "word/document.xml"} <= names:
            raise DocumentExtractionError("job_description_file_unreadable")
        if any(
            entry.filename.casefold().endswith(".zip")
            or package.open(entry).read(4) == b"PK\x03\x04"
            for entry in entries
            if not entry.is_dir()
        ):
            raise DocumentExtractionError("job_description_file_too_complex")
```

Traverse paragraphs and tables in document order and do not dereference external relationship targets:

```python
def _docx_text(data: bytes) -> str:
    try:
        _validate_docx_package(data)
        document = Document(BytesIO(data))
        blocks: list[str] = []
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                if text := block.text.strip():
                    blocks.append(text)
            elif isinstance(block, Table):
                for row in block.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        blocks.append("\t".join(cells))
        return "\n\n".join(blocks)
    except DocumentExtractionError:
        raise
    except (BadZipFile, PackageNotFoundError, OSError, ValueError, KeyError) as error:
        raise DocumentExtractionError("job_description_file_unreadable") from error
```

- [ ] **Step 6: Normalize and classify empty or overlong text**

```python
def _normalize_text(value: str) -> str:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned = "\n".join(
        "".join(character for character in line if character == "\t" or character.isprintable()).rstrip()
        for line in lines
    ).strip()
    if not cleaned:
        raise DocumentExtractionError("job_description_text_missing")
    if len(cleaned) > MAX_TEXT_LENGTH:
        raise DocumentExtractionError("job_description_text_too_long")
    return cleaned
```

- [ ] **Step 7: Run parser tests and static checks**

Run:

```bash
cd backend
uv run pytest tests/unit/jobs/test_document_extraction.py -v
uv run ruff check app/jobs/document_extraction.py tests/unit/jobs/test_document_extraction.py
uv run mypy app/jobs/document_extraction.py
```

Expected: all commands PASS.

- [ ] **Step 8: Commit the extraction core**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/jobs/document_extraction.py backend/tests/job_description_fixtures.py backend/tests/unit/jobs/test_document_extraction.py
git commit -m "feat: extract job descriptions from safe documents"
```

### Task 5: Authenticated Document Extraction API

**Files:**
- Create: `backend/app/jobs/document_router.py`
- Create: `backend/tests/integration/jobs/test_document_extraction_api.py`
- Modify: `backend/app/jobs/schemas.py`
- Modify: `backend/app/main.py:116-164`

**Interfaces:**
- Consumes: `JobDescriptionExtractor.extract(...)` from Task 4.
- Produces: `POST /api/v1/job-descriptions/extract`.
- Produces: `JobDescriptionExtractionResponse{text, source}` and documented stable error statuses.

- [ ] **Step 1: Write failing authenticated API tests**

```python
from tests.job_description_fixtures import readable_docx, readable_pdf


def test_pdf_extraction_returns_text_without_persisting_a_job(document_api):
    before = document_api["count_jobs"]()
    response = document_api["api"].post(
        "/api/v1/job-descriptions/extract",
        headers=document_api["headers"],
        files={"file": ("role.pdf", readable_pdf(), "application/pdf")},
    )
    assert response.status_code == 200
    assert "Senior Product Designer" in response.json()["text"]
    assert response.json()["source"] == {
        "filename": "role.pdf",
        "media_type": "application/pdf",
    }
    assert document_api["count_jobs"]() == before


def _post_files(document_api, parts):
    return document_api["api"].post(
        "/api/v1/job-descriptions/extract",
        headers=document_api["headers"],
        files=[("file", part) for part in parts],
    )


@pytest.mark.parametrize(
    ("parts", "status_code", "code"),
    [
        ([], 400, "job_description_file_required"),
        ([
            ("one.pdf", b"%PDF-1.4", "application/pdf"),
            ("two.pdf", b"%PDF-1.4", "application/pdf"),
        ], 400, "job_description_file_required"),
        ([("oversized.pdf", b"x" * 10_000_001, "application/pdf")], 413, "job_description_file_too_large"),
        ([("role.txt", b"plain text", "text/plain")], 415, "job_description_type_unsupported"),
        ([("corrupt.pdf", b"%PDF-1.4 corrupt", "application/pdf")], 422, "job_description_file_unreadable"),
    ],
)
def test_upload_errors_are_stable(document_api, parts, status_code, code):
    response = _post_files(document_api, parts)
    assert response.status_code == status_code
    assert response.json() == {"detail": {"code": code}}
```

Also test missing authentication, extractor timeout, extractor exception redaction, DOCX success, captured logs contain neither the filename nor extracted text, and that the fake `UploadFile.close()` is awaited on success and failure.

Build `document_api` by copying the in-memory SQLite, `StaticVerifier`, tenant, owner-membership, `get_db` override, and `apply_tenant_context` monkeypatch pattern from `backend/tests/integration/jobs/test_scorecard_versioning.py::job_api`. Construct the app with `create_app(Settings.for_test(), job_description_extractor=DefaultJobDescriptionExtractor())`. Yield `api`, authenticated `headers`, and a `count_jobs()` closure that opens a fresh `Session(engine)` and returns `select(func.count()).select_from(Job)`; this makes the no-persistence assertion explicit. Use a separately injected blocking extractor for the timeout test and a raising extractor for redaction and cleanup tests.

- [ ] **Step 2: Run the API tests and confirm the route is absent**

Run: `cd backend && uv run pytest tests/integration/jobs/test_document_extraction_api.py -v`

Expected: FAIL with HTTP 404 for `/api/v1/job-descriptions/extract`.

- [ ] **Step 3: Add response schemas and router dependency injection**

```python
class JobDescriptionSource(BaseModel):
    model_config = ConfigDict(frozen=True)
    filename: str
    media_type: str


class JobDescriptionExtractionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str = Field(min_length=1, max_length=50_000)
    source: JobDescriptionSource
```

In `create_app()`, accept `job_description_extractor: JobDescriptionExtractor | None = None`, store `DefaultJobDescriptionExtractor()` on `app.state`, and include `document_router` after the identity and clients routers.

- [ ] **Step 4: Implement bounded multipart reading, timeout, and cleanup**

```python
@router.post("/api/v1/job-descriptions/extract", response_model=JobDescriptionExtractionResponse)
async def extract_job_description(
    _context: Annotated[RequestContext, Depends(get_request_context)],
    extractor: Annotated[JobDescriptionExtractor, Depends(get_document_extractor)],
    files: Annotated[list[UploadFile] | None, File(alias="file")] = None,
) -> JobDescriptionExtractionResponse:
    uploads = files or []
    if len(uploads) != 1:
        for upload in uploads:
            await upload.close()
        _raise_document_error(DocumentExtractionError("job_description_file_required"))
    upload = uploads[0]
    try:
        data = await _read_at_most(upload, MAX_FILE_BYTES)
        async with asyncio.timeout(10):
            result = await asyncio.to_thread(
                extractor.extract,
                data=data,
                filename=upload.filename or "",
                media_type=upload.content_type,
            )
    except TimeoutError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "job_description_extraction_unavailable"},
        ) from error
    except DocumentExtractionError as error:
        _raise_document_error(error)
    finally:
        await upload.close()
    return JobDescriptionExtractionResponse(
        text=result.text,
        source={"filename": result.filename, "media_type": result.media_type},
    )
```

Implement `_read_at_most()` in 64 KiB chunks and stop immediately after reading byte 10,000,001. `_raise_document_error()` must use the exact status mapping from the spec and return only `{"detail":{"code":...}}`.

```python
_DOCUMENT_ERROR_STATUS = {
    "job_description_file_required": 400,
    "job_description_file_too_large": 413,
    "job_description_type_unsupported": 415,
    "job_description_file_unreadable": 422,
    "job_description_text_missing": 422,
    "job_description_text_too_long": 422,
    "job_description_file_too_complex": 422,
    "job_description_extraction_unavailable": 503,
}
```

- [ ] **Step 5: Run API, OpenAPI, and existing jobs tests**

Run:

```bash
cd backend
uv run pytest tests/integration/jobs/test_document_extraction_api.py tests/integration/jobs tests/unit/jobs -v
uv run python -c 'from app.main import create_app; assert "/api/v1/job-descriptions/extract" in create_app().openapi()["paths"]'
```

Expected: PASS, with no job or scorecard row created by extraction.

- [ ] **Step 6: Commit the API boundary**

```bash
git add backend/app/jobs/document_router.py backend/app/jobs/schemas.py backend/app/main.py backend/tests/integration/jobs/test_document_extraction_api.py
git commit -m "feat: expose authenticated job description extraction"
```

### Task 6: Bounded Multipart BFF Boundary

**Files:**
- Create: `web/lib/document-extraction-bff.ts`
- Create: `web/app/api/bff/job-descriptions/extract/route.ts`
- Create: `web/tests/security/document-extraction-bff.test.ts`
- Modify: `web/lib/bff.ts:41-56`
- Modify: `web/lib/schemas.ts`
- Modify: `web/lib/generated-api.ts` through the generator

**Interfaces:**
- Consumes: backend endpoint and generated `JobDescriptionExtractionResponse` from Task 5.
- Produces: `handleDocumentExtraction(request, dependencies)` and `/api/bff/job-descriptions/extract`.
- Preserves: origin validation, selected tenant, caller abort signal, a stable idempotency key, no-store responses, and safe upstream codes.

- [ ] **Step 1: Generate the new API types**

Run: `cd web && npm run api:generate`

Expected: `web/lib/generated-api.ts` contains `JobDescriptionExtractionResponse`, `JobDescriptionSource`, `SeniorityLevel`, `SeniorityOption`, and `ScorecardDraftResponse.seniority_options`.

- [ ] **Step 2: Write failing BFF boundary tests**

```typescript
function uploadRequest(form: FormData): Request {
  return new Request("https://sourcing.example.com/api/bff/job-descriptions/extract", {
    method: "POST",
    headers: {
      "Idempotency-Key": "extract-intent",
      Origin: "https://sourcing.example.com",
      "Sec-Fetch-Site": "same-origin",
    },
    body: form,
  })
}


it("forwards exactly one bounded file without persisting or JSON-encoding it", async () => {
  const file = new File(["%PDF-1.4 safe"], "role.pdf", { type: "application/pdf" })
  const form = new FormData()
  form.set("file", file)
  const callApi = vi.fn().mockResolvedValue({
    text: "Senior Product Designer",
    source: { filename: "role.pdf", media_type: "application/pdf" },
  })
  const response = await handleDocumentExtraction(uploadRequest(form), {
    appUrl: "https://sourcing.example.com",
    readTenant: async () => tenantId,
    callApi,
  })
  expect(response.status).toBe(200)
  const init = callApi.mock.calls[0][2]
  expect(init.body).toBeInstanceOf(FormData)
  expect((init.body as FormData).getAll("file")).toHaveLength(1)
  expect(init.headers).toBeUndefined()
})
```

Add tests for cross-origin requests, missing/invalid idempotency key, missing tenant, missing file, two files, an unexpected form field, file byte sizes at 10,000,000 and 10,000,001, unsupported extension/type, upstream 413/415/422/503 code preservation, and caller abort forwarding.

- [ ] **Step 3: Run the BFF tests and confirm the helper is absent**

Run: `cd web && npm test -- --run tests/security/document-extraction-bff.test.ts`

Expected: FAIL because `@/lib/document-extraction-bff` does not exist.

- [ ] **Step 4: Implement the specialized upload boundary**

```typescript
type DocumentExtractionDependencies = {
  appUrl: string
  readTenant?: () => Promise<string | null>
  callApi?: (
    path: string,
    tenantId: string,
    init: ApiInit,
  ) => Promise<unknown>
}


export async function handleDocumentExtraction(
  request: Request,
  dependencies: DocumentExtractionDependencies,
): Promise<Response> {
  try {
    assertMutationOrigin(request, dependencies.appUrl)
  } catch {
    return bffErrorResponse("invalid_request_origin", 403)
  }
  const idempotencyKey = request.headers.get("Idempotency-Key")?.trim()
  if (!idempotencyKey || idempotencyKey.length > 200) {
    return bffErrorResponse("idempotency_key_required", 400)
  }
  if (!(request.headers.get("Content-Type") ?? "").toLowerCase().startsWith("multipart/form-data")) {
    return bffErrorResponse("job_description_type_unsupported", 415)
  }
  let form: FormData
  try {
    form = await request.formData()
  } catch {
    return bffErrorResponse("job_description_file_required", 400)
  }
  if ([...form.keys()].some((key) => key !== "file")) {
    return bffErrorResponse("job_description_file_required", 400)
  }
  const files = form.getAll("file").filter((value): value is File => value instanceof File)
  if (files.length !== 1) return bffErrorResponse("job_description_file_required", 400)
  if (files[0].size > 10_000_000) return bffErrorResponse("job_description_file_too_large", 413)
  const upstream = new FormData()
  upstream.set("file", files[0], files[0].name)
  let tenantId: string | null
  try {
    tenantId = await (dependencies.readTenant ?? selectedTenantId)()
  } catch {
    return bffErrorResponse("tenant_unavailable", 503)
  }
  if (!tenantId) return bffErrorResponse("tenant_required", 401)
  try {
    const result = await (dependencies.callApi ?? apiFetch)(
      "/api/v1/job-descriptions/extract",
      tenantId,
      {
        method: "POST",
        body: upstream,
        idempotencyKey,
        signal: request.signal,
        timeoutMs: 12_000,
      },
    )
    return Response.json(result, {
      headers: { "Cache-Control": "private, no-store" },
    })
  } catch (error) {
    if (error instanceof ApiError) {
      return bffErrorResponse(error.code, bffPublicStatus(error))
    }
    return bffErrorResponse("api_unavailable", 502)
  }
}
```

Export `bffPublicStatus(error)` from `web/lib/bff.ts` and allow the documented 413, 415, 422, and 503 statuses. Keep the existing fallback of 502 for unknown upstream failures.

- [ ] **Step 5: Add the route adapter and exported response type**

```typescript
// web/app/api/bff/job-descriptions/extract/route.ts
import { handleDocumentExtraction } from "@/lib/document-extraction-bff"

export async function POST(request: Request): Promise<Response> {
  const appUrl = process.env.AUTH_URL
  if (!appUrl) return Response.json(
    { code: "authentication_configuration_invalid" },
    { status: 503, headers: { "Cache-Control": "private, no-store" } },
  )
  return handleDocumentExtraction(request, { appUrl })
}
```

Export these generated types from `web/lib/schemas.ts`:

```typescript
export type JobDescriptionExtraction =
  components["schemas"]["JobDescriptionExtractionResponse"]
export type SeniorityLevel = components["schemas"]["SeniorityLevel"]
export type SeniorityOption = components["schemas"]["SeniorityOption"]
```

- [ ] **Step 6: Run security, API-contract, type, and lint checks**

Run:

```bash
cd web
npm test -- --run tests/security/document-extraction-bff.test.ts tests/security/bff.test.ts tests/security/api.test.ts
npm run api:check
npm run typecheck
npm run lint
```

Expected: all commands PASS.

- [ ] **Step 7: Commit the BFF boundary**

```bash
git add web/lib/document-extraction-bff.ts web/app/api/bff/job-descriptions/extract/route.ts web/tests/security/document-extraction-bff.test.ts web/lib/bff.ts web/lib/schemas.ts web/lib/generated-api.ts
git commit -m "feat: proxy bounded job description uploads"
```

### Task 7: Recruiter Upload and Editable Text Review

**Files:**
- Create: `web/components/jobs/JobDescriptionUpload.tsx`
- Create: `web/tests/jobs/job-description-upload.test.tsx`
- Modify: `web/components/jobs/JobIntakeForm.tsx:36-195`
- Modify: `web/tests/jobs/job-intake.test.tsx`
- Modify: `web/app/globals.css`

**Interfaces:**
- Consumes: `/api/bff/job-descriptions/extract` and `JobDescriptionExtraction` from Task 6.
- Produces: `JobDescriptionUpload({currentText, disabled, onBusyChange, onExtracted})`.
- Preserves: existing create-job then generate-scorecard sequence, using only reviewed text.

- [ ] **Step 1: Write failing component tests**

```typescript
it("extracts a PDF into the editable job-description field", async () => {
  server.use(http.post("/api/bff/job-descriptions/extract", () =>
    HttpResponse.json({
      text: "Senior Product Designer\nLead growth design.",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }),
  ))
  render(<JobIntakeForm clients={authorizedClientsFixture} />)
  await userEvent.upload(
    screen.getByLabelText("Upload job description"),
    new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
  )
  expect(await screen.findByLabelText("Job description")).toHaveValue(
    "Senior Product Designer\nLead growth design.",
  )
  expect(screen.getByText("Extracted from role.pdf")).toBeVisible()
})


it("asks before replacing existing text and cancellation avoids the request", async () => {
  const called = vi.fn()
  server.use(http.post("/api/bff/job-descriptions/extract", called))
  render(<JobIntakeForm clients={authorizedClientsFixture} />)
  await userEvent.type(screen.getByLabelText("Job description"), "Keep this text")
  const file = new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" })
  await userEvent.upload(screen.getByLabelText("Upload job description"), file)
  expect(screen.getByRole("dialog", { name: "Replace job description?" })).toBeVisible()
  await userEvent.click(screen.getByRole("button", { name: "Keep existing text" }))
  expect(screen.getByLabelText("Job description")).toHaveValue("Keep this text")
  expect(called).not.toHaveBeenCalled()
})
```

Add tests for DOCX acceptance, unsupported client-side type, progress status, generation disabled while extracting, all eight stable error messages, successful retry with the same intent key, and no filename included in the create-job JSON.

- [ ] **Step 2: Run intake tests and confirm upload UI is absent**

Run: `cd web && npm test -- --run tests/jobs/job-description-upload.test.tsx tests/jobs/job-intake.test.tsx`

Expected: FAIL because no upload control or extraction request exists.

- [ ] **Step 3: Implement the focused upload component**

```typescript
type JobDescriptionUploadProps = {
  currentText: string
  disabled: boolean
  onBusyChange: (busy: boolean) => void
  onExtracted: (result: JobDescriptionExtraction) => void
}

const extractionMessages: Record<string, string> = {
  job_description_file_required: "Choose one PDF or DOCX job description.",
  job_description_file_too_large: "The job description file must be 10 MB or smaller.",
  job_description_type_unsupported: "Upload a PDF or DOCX job description.",
  job_description_file_unreadable: "The uploaded job description file is corrupted or might be password-protected.",
  job_description_text_missing: "No readable text was found. Upload a text-based document or paste the job description.",
  job_description_text_too_long: "The extracted job description is too long. Paste a shortened version of 50,000 characters or fewer.",
  job_description_file_too_complex: "The job description could not be processed safely. Upload a simpler file or paste the text.",
  job_description_extraction_unavailable: "The job description could not be extracted. Try again safely or paste the text.",
}
```

Use `ModalDialog` for replacement confirmation. Generate one UUID per selected-file intent, reuse it for a retry of that same file, send `FormData` plus `Idempotency-Key`, focus the error alert on failure, and clear the native file input after cancellation or completion so selecting the same file again triggers `change`.

- [ ] **Step 4: Integrate with React Hook Form without creating a second source of truth**

Destructure `setValue` and `watch` from `useForm`. Pass `watch("jobDescription")` as `currentText`. On extraction call:

```typescript
setValue("jobDescription", result.text, {
  shouldDirty: true,
  shouldTouch: true,
  shouldValidate: true,
})
setExtractedSource(result.source.filename)
```

Track `extracting` in the parent through `onBusyChange` and disable “Generate scorecard” when `isSubmitting || extracting`. Submit the existing `values.jobDescription`; do not add source metadata to `JobIntakeValues` or the create-job body.

- [ ] **Step 5: Run component, accessibility, and intake regression tests**

Run:

```bash
cd web
npm test -- --run tests/jobs/job-description-upload.test.tsx tests/jobs/job-intake.test.tsx
npm run test:a11y
npm run typecheck
```

Expected: PASS; paste-only tests remain unchanged.

- [ ] **Step 6: Commit the recruiter upload flow**

```bash
git add web/components/jobs/JobDescriptionUpload.tsx web/components/jobs/JobIntakeForm.tsx web/tests/jobs/job-description-upload.test.tsx web/tests/jobs/job-intake.test.tsx web/app/globals.css
git commit -m "feat: review extracted job descriptions before generation"
```

### Task 8: Seniority Presets and Explicit Custom Override UI

**Files:**
- Modify: `web/lib/request-schemas.ts:48-69`
- Modify: `web/components/scorecards/ScorecardEditor.tsx:67-155`
- Modify: `web/components/scorecards/ScorecardEditor.tsx:413-500`
- Modify: `web/tests/scorecards/scorecard-editor.test.tsx`
- Modify: `web/tests/fixtures.ts`
- Modify: `web/components/dev/Task13Preview.tsx`
- Modify: `web/app/globals.css`

**Interfaces:**
- Consumes: generated `SeniorityLevel`, `SeniorityOption`, and `ScorecardDraftResponse.seniority_options` from Tasks 1 and 6.
- Produces: zero-to-three checkboxes and a frontend-only `customEnabled` state; the saved API payload remains `seniority`, `minimum_years`, and `maximum_years`.

- [ ] **Step 1: Tighten the web request schema and write failing UI tests**

```typescript
const seniorityLevel = z.enum(["early_career", "mid_level", "senior"])

export const scorecardDraftRequest = z.object({
  target_titles: z.array(z.string().trim().min(1)).min(1).max(12),
  criteria: z.array(scorecardCriterionRequest).min(1).max(40),
  seniority: z.array(seniorityLevel).max(3),
  minimum_years: z.number().int().min(0).max(50).nullable(),
  maximum_years: z.number().int().min(0).max(50).nullable(),
  locations: z.array(z.string().trim().min(1)).max(20),
  industry_code: z.string().trim().min(1).max(128),
  suggested_adjacent_industries: z.array(z.string().trim().min(1).max(128)).max(12),
  uncertainties: z.array(z.string().trim().min(1)).max(20),
  confirmed_inferred_items: z.array(z.string().min(1)).max(72),
}).strict().refine(
  (value) =>
    value.minimum_years === null ||
    value.maximum_years === null ||
    value.minimum_years <= value.maximum_years,
)
```

```typescript
function renderEditor(
  overrides: Partial<typeof scorecardDraftFixture.draft>,
): void {
  const draft = { ...scorecardDraftFixture.draft, ...overrides }
  render(
    <ScorecardEditor
      draft={{
        ...scorecardDraftFixture,
        draft: {
          ...draft,
          confirmed_inferred_items: requiredInferenceIds(draft),
        },
      }}
      allowedIndustryCodes={["technology.fintech"]}
    />,
  )
}


it("supports zero, one, or multiple canonical presets", async () => {
  renderEditor({ seniority: [], minimum_years: null, maximum_years: null })
  const early = screen.getByRole("checkbox", { name: "Early-Career — 0–3 years" })
  const mid = screen.getByRole("checkbox", { name: "Mid-Level — 3–9 years" })
  await userEvent.click(early)
  await userEvent.click(mid)
  expect(early).toBeChecked()
  expect(mid).toBeChecked()
  expect(screen.getByRole("checkbox", { name: "Senior — 10+ years" })).not.toBeChecked()
})


it("requires a bound and makes custom range visibly override presets", async () => {
  renderEditor({ seniority: ["early_career"], minimum_years: null, maximum_years: null })
  await userEvent.click(screen.getByRole("checkbox", { name: "Use custom experience range" }))
  expect(screen.getByText("This custom range overrides the selected seniority levels.")).toBeVisible()
  expect(screen.getByRole("checkbox", { name: "Early-Career — 0–3 years" })).toBeDisabled()
  expect(screen.getByRole("button", { name: "Confirm and source" })).toBeDisabled()
  await userEvent.type(screen.getByLabelText("Minimum years"), "5")
  expect(screen.getByRole("button", { name: "Confirm and source" })).toBeEnabled()
})
```

Also test maximum-only, bounded range ordering, preset preservation while custom is active, clearing both bounds when override turns off, and control locking during submission.

Add a legacy-draft test with `seniority: ["manager"]`. The editor must display “Unrecognized historical seniority: manager,” disable confirmation, and provide a “Remove manager” button. Removing it leaves a valid zero-preset draft; do not silently map it to a canonical level.

- [ ] **Step 2: Run editor tests and verify the free-form input fails them**

Run: `cd web && npm test -- --run tests/scorecards/scorecard-editor.test.tsx`

Expected: FAIL because the editor still renders one free-form seniority input and always-visible year fields.

- [ ] **Step 3: Replace free-form seniority with server-provided options**

Remove `seniorityInput`. Initialize `customEnabled` from either non-null bound. Render `response.seniority_options`:

```typescript
{response.seniority_options.map((option) => {
  const checked = draft.seniority.includes(option.value)
  const range = option.maximum_years === null
    ? `${option.minimum_years}+ years`
    : `${option.minimum_years}–${option.maximum_years} years`
  return (
    <label className="check-row" key={option.value}>
      <input
        type="checkbox"
        checked={checked}
        disabled={submitting || customEnabled}
        onChange={() => setDraft((current) => ({
          ...current,
          seniority: checked
            ? current.seniority.filter((value) => value !== option.value)
            : [...current.seniority, option.value],
        }))}
      />
      {option.label} — {range}
    </label>
  )
})}
```

Sort submitted selections in server-provided option order, not click order.

Compute unrecognized historical values from the option set, include them in `structurallyValid`, and render an explicit removal action:

```typescript
const canonicalSeniority = new Set(
  response.seniority_options.map((option) => option.value),
)
const unrecognizedSeniority = draft.seniority.filter(
  (value) => !canonicalSeniority.has(value as SeniorityLevel),
)

{unrecognizedSeniority.map((value) => (
  <div className="field-error" key={value} role="alert">
    <span>Unrecognized historical seniority: {value}</span>
    <button
      type="button"
      disabled={submitting}
      onClick={() => setDraft((current) => ({
        ...current,
        seniority: current.seniority.filter((item) => item !== value),
      }))}
    >
      Remove {value}
    </button>
  </div>
))}
```

Require `unrecognizedSeniority.length === 0` in `structurallyValid`.

- [ ] **Step 4: Implement the explicit custom-range control**

Render the numeric fields only when `customEnabled`. Enabling with no bounds makes `structurallyValid` false. Disabling executes:

```typescript
setCustomEnabled(false)
setDraft((current) => ({
  ...current,
  minimum_years: null,
  maximum_years: null,
}))
intent.current = null
```

Keep the selected preset values in state while controls are disabled. Display the exact override sentence with `role="status"`.

- [ ] **Step 5: Update fixtures and run editor regressions**

Add all three `seniority_options` to `scorecardDraftFixture`, `manualRequiredDraftFixture`, and Task13 preview draft responses. Replace new-draft `manager` values with canonical levels. Keep historical candidate/provider labels unchanged.

Run:

```bash
cd web
npm test -- --run tests/scorecards tests/jobs
npm run typecheck
npm run lint
```

Expected: PASS.

- [ ] **Step 6: Commit the editor behavior**

```bash
git add web/lib/request-schemas.ts web/components/scorecards/ScorecardEditor.tsx web/tests/scorecards/scorecard-editor.test.tsx web/tests/fixtures.ts web/components/dev/Task13Preview.tsx web/app/globals.css
git commit -m "feat: add scorecard seniority presets and overrides"
```

### Task 9: End-to-End Coverage, Evaluation, and Release Verification

**Files:**
- Modify: `web/e2e/task-13-review.spec.ts`
- Modify: `web/tests/review-fixtures.ts`
- Modify: `evaluation/fixtures/synthetic_jobs.jsonl` only if canonical draft validation requires it
- Modify: `evaluation/fixtures/synthetic-baseline.json` only if the evaluation command reports intentional matching-v2 changes
- Modify: `evaluation/fixtures/baseline.json` only if the evaluation command reports intentional matching-v2 changes

**Interfaces:**
- Consumes: all prior tasks.
- Produces: executable acceptance coverage and a clean full-project verification run.

- [ ] **Step 1: Extend the deterministic BFF interceptor for extraction**

```typescript
if (pathname.endsWith("/job-descriptions/extract") && method === "POST") {
  await json({
    text: "Senior Product Designer\nLead product design for the growth team.",
    source: { filename: "role.pdf", media_type: "application/pdf" },
  })
  return
}
```

Record that extraction occurred, but do not record multipart content or filenames in the fake backend's observed telemetry payload.

- [ ] **Step 2: Add PDF and DOCX recruiter-flow scenarios**

```typescript
for (const file of [
  { name: "role.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4") },
  { name: "role.docx", mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: Buffer.from("PK\\x03\\x04") },
]) {
  test(`uploaded ${file.name} populates editable text before generation`, async ({ page }) => {
    await page.goto("/jobs/new")
    await page.getByLabel("Upload job description").setInputFiles(file)
    await expect(page.getByLabel("Job description")).toHaveValue(
      "Senior Product Designer\nLead product design for the growth team.",
    )
    await page.getByLabel("Job description").fill(
      "Senior Product Designer\nLead growth product design.",
    )
    await page.getByLabel("Client").selectOption(clientId)
    await page.getByLabel("Job title").fill("Senior Product Designer")
    await page.getByRole("button", { name: "Generate scorecard" }).click()
    await expect(page.getByRole("heading", { level: 1, name: "Review scorecard" })).toBeVisible()
  })
}
```

Use the existing helper constants for client and title values. Add a replacement-cancellation assertion and a scorecard-editor assertion for selecting Early-Career plus Mid-Level and then activating a minimum-only override.

Add two intercepted failure scenarios. Return `job_description_file_unreadable` with HTTP 422 for `encrypted.pdf` and assert the exact shared corrupted/password-protected message. Return `job_description_text_missing` with HTTP 422 for `scan.pdf` and assert the paste-fallback message; assert no OCR request or job-creation request is observed in either scenario.

- [ ] **Step 3: Run focused end-to-end tests**

Run: `cd web && npx playwright test e2e/task-13-review.spec.ts`

Expected: PASS for Chromium with both upload variants and the existing paste-only path.

- [ ] **Step 4: Run matching evaluation and update only intentional baselines**

Run from repository root:

```bash
uv run --project backend pytest evaluation/test_evaluation_schema.py -v
uv run --project backend python evaluation/evaluate_matching.py
```

Expected: schema tests PASS. If the evaluator reports differences caused by numeric-only missing-years behavior, inspect every changed case, update the relevant baseline JSON with the repository's evaluator workflow, and rerun until PASS. Do not update a baseline to hide an unexplained regression.

- [ ] **Step 5: Run the complete backend verification suite**

```bash
cd backend
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -v --cov=app --cov-report=term-missing --cov-fail-under=90
```

Expected: every command exits 0 and coverage is at least 90%.

- [ ] **Step 6: Run the complete web verification suite**

```bash
cd web
npm run api:check
npm run lint
npm run typecheck
npm test -- --run
npm run test:a11y
npm run e2e
npm run e2e:production-preview
```

Expected: every command exits 0.

- [ ] **Step 7: Verify persistence and privacy invariants manually from tests**

Run:

```bash
rg -n "filename|media_type|uploaded.*bytes|job.description.file" backend/app/jobs backend/app/core web/lib web/components/jobs
git diff --check
git status --short
```

Expected: filename/media type occur only in request/response/UI state, no model or migration stores them, no log call includes them, `git diff --check` is clean, and only intended files are modified.

- [ ] **Step 8: Commit end-to-end and evaluation updates**

```bash
git add web/e2e/task-13-review.spec.ts web/tests/review-fixtures.ts evaluation/fixtures/synthetic_jobs.jsonl evaluation/fixtures/synthetic-baseline.json evaluation/fixtures/baseline.json
git diff --cached --quiet || git commit -m "test: cover document intake and seniority improvements"
```

If none of the evaluation or fixture files changed, stage only `web/e2e/task-13-review.spec.ts` and commit that file.

## Completion Gate

Before claiming implementation complete, invoke `superpowers:verification-before-completion`, rerun the commands in Task 9 against the final commit, inspect their current output, and report any intentionally deferred follow-on work from the spec. Do not begin active-scorecard editing, candidate resume work, candidate-detail reordering, or evidence-tag work in this implementation cycle.
