# Task 7 report: deterministic matching and near matches

## Status

`DONE`

## Commits

- `c5a4b72 feat: add deterministic candidate matching`
- The follow-up documentation commit contains this report and is recorded in the task handoff.

## Files changed

- `backend/app/candidates/schemas.py`
- `backend/app/matching/__init__.py`
- `backend/app/matching/schemas.py`
- `backend/app/matching/engine.py`
- `backend/app/matching/explanations.py`
- `backend/tests/unit/matching/conftest.py`
- `backend/tests/unit/matching/test_hard_gates.py`
- `backend/tests/unit/matching/test_scoring.py`
- `backend/tests/property/test_matching_determinism.py`
- `.superpowers/sdd/2026-08-15-recruitment-sourcing-agent-vertical-slice/task-7-report.md`

## Design and assumptions

- `MatchingEngine.evaluate(scorecard, candidate)` is synchronous, deterministic, and local. It loads only the checked-in `IndustryTaxonomy` resource. It does not import or call a language model, provider, HTTP client, network API, or protected-characteristic classifier.
- The existing `CandidateProfile` DTO was extended compatibly with defaulted explicit evidence facts: `skills=()`, `industry_codes=()`, `seniority=None`, `years_experience=None`, and `work_eligibility=None`. Existing Task 6 callers and ORM profiles therefore remain valid and expose these facts as unknown until a later ingestion/enrichment task explicitly stores them. No candidate persistence or provider mapping was changed.
- The five component maxima are fixed exactly at role/skills `35`, scope/seniority/years `25`, industry `20`, location/work eligibility `10`, and recency/trajectory `10`. `ScoreBreakdown` enforces each bound, `MatchResult` enforces `total == sum(breakdown)`, and no component is reallocated to compensate for unknown evidence.
- Role/skills, scope, and location/eligibility divide their fixed component maximum deterministically across the configured atomic evaluations. Only `SUPPORTED` atoms receive their assigned points. Unknown and failed atoms receive zero; removing unknown atoms does not move their points to supported atoms.
- Explicit title, skill, seniority, location, and work-eligibility aliases are normalized locally. Equivalent input collections are sorted and deduplicated using deterministic representatives, including case/whitespace duplicates. Synthetic component evaluations use a `component.` namespace, which cannot collide with the scorecard criterion key regex.
- Every confirmed criterion is stored as a `CriterionEvaluation` with state, summary, points, maximum, explicit evidence, and source references. A failed `MUST_HAVE` produces `near_match`. An unknown `MUST_HAVE` produces `near_match` only when `evidence_required=True`; otherwise it remains `main` with zero points and a visible `unknown_keys` entry.
- Skill absence is `UNKNOWN` when the explicit candidate skill set is empty. When a non-empty explicit skill set is available, a missing required normalized skill is `FAILED`; this is the binding evidence semantics requested for Task 7.
- Work-eligibility criteria are routed with the existing authoritative `DEFAULT_SCORECARD_LEGAL_POLICY` across key, label, and source text. Matching then uses only the candidate's explicit `work_eligibility` fact and explicit US/India/sponsorship aliases. Name, company, location, citizenship, nationality, or other profile fields never supply work-eligibility evidence.
- Exact candidate industry receives `20`. A candidate industry receives `12` (60%) only when it is both present in the confirmed scorecard's recruiter-approved adjacent list and a valid adjacency edge in taxonomy v1. Unrelated known industry receives zero/failed; absent or unrecognized evidence receives zero/unknown.
- Recency uses explicit experience end dates and treats an explicit `current`, `now`, or `present` marker as current evidence. Otherwise, the latest parsed end year is recent within three years of scorecard confirmation. Trajectory compares dated, explicitly titled experience levels in stable chronological order. Missing comparable facts remain unknown.
- `format_explanation` only partitions and returns the summaries already stored on `MatchResult.criteria`; it calculates no evidence and introduces no new candidate fact.

## RED tests and exact observed failures

### Requested launcher

```sh
cd backend && uv run pytest tests/unit/matching tests/property/test_matching_determinism.py -v
```

The repository environment does not have `uv` on `PATH`:

```text
zsh:1: command not found: uv
```

The committed backend virtual environment was used, matching prior tasks.

### Initial matching RED

```sh
cd backend && ./.venv/bin/pytest tests/unit/matching tests/property/test_matching_determinism.py -v
```

Observed before implementation:

```text
ImportError while loading conftest '.../backend/tests/unit/matching/conftest.py'.
tests/unit/matching/conftest.py:14: in <module>
    from app.matching.engine import MatchingEngine
E   ModuleNotFoundError: No module named 'app.matching'
```

### Independent-review regression RED

After the initial implementation, the reviewer identified eligibility routing, evaluation-key collision, and input-order leaks. Four tests were added before fixes:

```sh
cd backend && ./.venv/bin/pytest \
  tests/unit/matching/test_hard_gates.py::test_source_text_only_work_eligibility_is_not_evaluated_as_a_skill \
  tests/unit/matching/test_hard_gates.py::test_equivalent_explicit_work_eligibility_phrases_match \
  tests/unit/matching/test_scoring.py::test_recruiter_criterion_cannot_be_hidden_by_a_component_key_collision \
  tests/property/test_matching_determinism.py::test_normalized_duplicate_spellings_and_tied_dates_are_deterministic -v
```

Exact failure summary:

```text
FAILED test_source_text_only_work_eligibility_is_not_evaluated_as_a_skill
assert ('candidate_requirement',) == ()

FAILED test_equivalent_explicit_work_eligibility_phrases_match
assert 'near_match' == 'main'

FAILED test_recruiter_criterion_cannot_be_hidden_by_a_component_key_collision
StopIteration while locating key == 'component.industry'

FAILED test_normalized_duplicate_spellings_and_tied_dates_are_deterministic
assert actual == baseline

4 failed in 0.32s
```

Each fix was applied and its focused regression passed before moving to the next finding.

### Source-text eligibility comparison RED

The fix-round reviewer then found that routing was corrected but source-text phrases still required whole-string alias equality. The explicit regression was written first:

```sh
cd backend && ./.venv/bin/pytest \
  tests/unit/matching/test_hard_gates.py::test_source_text_work_eligibility_matches_explicit_candidate_fact -v
```

Observed before phrase-aware canonicalization:

```text
FAILED test_source_text_work_eligibility_matches_explicit_candidate_fact
assert 'near_match' == 'main'
1 failed in 0.08s
```

The alias matcher now recognizes the longest explicit phrase within requirement text, with longer negative forms evaluated before contained positive forms. The exact regression then passed.

## GREEN and final verification

### Focused matching suite

```sh
cd backend && ./.venv/bin/pytest \
  tests/unit/matching tests/property/test_matching_determinism.py -v
```

Final output:

```text
20 passed in 0.23s
```

This includes focused examples for fixed weights, score sums, failed and unknown mandatory semantics, title/skill/seniority/location/work-eligibility aliases, exact/adjacent/unrelated/unknown industry, recruiter-key collision, evidence-only explanations, and score bounds.

### Property-test evidence

```sh
cd backend && ./.venv/bin/pytest \
  tests/property/test_matching_determinism.py -q --hypothesis-show-statistics
```

Final output:

```text
3 passed in 0.21s

test_reordered_equivalent_inputs_produce_identical_results:
48 passing, 0 failing, 1 invalid

test_normalized_duplicate_spellings_and_tied_dates_are_deterministic:
4 passing, 0 failing, 0 invalid

test_scores_remain_bounded_and_equal_the_fixed_component_sum:
100 passing, 0 failing, 18 invalid
```

The first property permutes skills, industry codes, experiences, and confirmed criteria and asserts identical classification, total, breakdown, stored evaluations, and explanation. The second exercises normalized duplicate spellings and tied experience dates. The third generates missing, matching, and conflicting component evidence and asserts every component bound, total `0..100`, and exact total/component sum equality.

### Full backend regression

The valid PostgreSQL owner URL was supplied for identity tests, and the available Task 6 PostgreSQL database enabled its five opt-in checks:

```sh
cd backend && \
TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/sourcing_test' \
TASK6_OWNER_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/sourcing_task6_candidate_identity' \
TASK6_API_DATABASE_URL='postgresql+psycopg://sourcing_api:replace-with-api-password@localhost:5432/sourcing_task6_candidate_identity' \
./.venv/bin/pytest -q
```

Final output:

```text
119 passed, 3 skipped in 2.37s
```

The three skips are the pre-existing Task 4 PostgreSQL concurrency tests, which require their separate opt-in database URLs. An attempted opt-in confirmed that the historical `sourcing_task4_fix2` database no longer exists, so those tests were left in their repository-defined skipped state.

### Static and diff verification

```sh
cd backend && ./.venv/bin/ruff format --check .
cd backend && ./.venv/bin/ruff check .
cd backend && ./.venv/bin/mypy app
cd backend && git diff --cached --check
```

Final output:

```text
69 files already formatted
All checks passed!
Success: no issues found in 40 source files
git diff --cached --check: no output, exit 0
```

## Self-review

- Re-read the Task 7 brief and approved matching-model design after implementation. Every changed production line is limited to the matching module or the backward-compatible candidate evidence fields needed by its frozen interface.
- Verified all five component maxima are exact and enforced independently; no path alters, calibrates, or renormalizes the approved 100-point model.
- Verified all points originate from `SUPPORTED` atomic evaluations. Unknown and failed evidence always receives zero, and total is validated as the exact component sum.
- Verified must-have classification is derived only from stored criterion state plus `evidence_required`, and `failed_must_haves`/`unknown_keys` are sorted deterministically.
- Verified exact industry wins over adjacency under reordered multi-industry inputs, adjacency requires both recruiter approval and taxonomy support, and partial credit is exactly `12/20`.
- Verified work eligibility is never derived from location, employer, name, citizenship, nationality, protected facts, or provider assumptions. The only candidate source is the explicit `work_eligibility` field.
- Verified every confirmed criterion remains visible even when it uses names such as `industry`; synthetic components cannot collide because their keys contain `component.` while scorecard keys cannot contain a period.
- Verified stored evidence, summaries, criteria ordering, breakdown, classification, and explanation are stable under input permutation, normalized duplicates, and tied experience dates.
- Verified explanation generation is a pure partition over stored summaries and has no provider, LLM, prompt, or network dependency.
- Mutation check: changing any component maximum, adjacency credit, supported/failed/unknown branch, mandatory-unknown gate, alias canonicalization, total formula, evaluation key namespace, or stable sorting breaks at least one focused or property test.
- Independent staged-diff review initially reported three Important findings. All received focused RED regressions and fixes. A second round found one residual source-text eligibility equality issue; it also received a RED regression and fix. Final reviewer verdict: no Critical or Important issues; ready.

## Concerns

- `uv` is unavailable in this environment, so verification uses the committed `backend/.venv` executables.
- Three pre-existing Task 4 PostgreSQL concurrency tests remain skipped because their opt-in disposable database is not present. All other backend tests, including the five available Task 6 PostgreSQL checks, ran.
- Task 7 adds normalized candidate evidence fields to the DTO only. Existing persistence/provider ingestion intentionally leaves them at unknown defaults until later enrichment work supplies explicit facts.
- Matching v1 intentionally uses explicit, versioned alias dictionaries plus deterministic three-year recency and title-level trajectory rules. New jurisdictions, eligibility phrasings, or calibrated internal allocations require an explicit matching-version change and regression fixtures rather than runtime inference.
