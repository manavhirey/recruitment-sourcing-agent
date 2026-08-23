# Scorecard Creation and Seniority Improvements Design

**Date:** 2026-08-23

**Status:** Design approved; written-spec review pending

**Parent request:** `docs/feature-requests/recruitment-scorecard-improvements.md`

## 1. Purpose

Reduce manual effort during scorecard creation by allowing a recruiter to extract a job description from a PDF or DOCX file, review and edit the extracted text, and generate a scorecard from that reviewed content. Replace free-form seniority handling with consistent presets, optional multi-selection, and an explicit custom experience-range override.

This design covers only scorecard creation and seniority. Candidate resume access, candidate-detail layout, evidence-based candidate tags, active-scorecard editing, and conditional rescoring are separate subprojects.

## 2. Product Decisions

- Pasted job-description text remains supported.
- A recruiter may either paste text or upload one PDF or DOCX file.
- Upload complements the text area; it does not create a separate source of truth.
- Successful extraction fills the existing editable job-description field.
- The recruiter reviews the extracted text before creating the job or generating a scorecard.
- The original file is request-scoped and is discarded on every success or failure path. It is never persisted.
- Image-only PDFs are not processed with OCR in this slice.
- A file may contain at most 10,000,000 bytes.
- The three seniority presets are Early-Career, Mid-Level, and Senior.
- A recruiter may select multiple presets or no preset.
- Early-Career and Mid-Level both include candidates with exactly three years of experience.
- A custom minimum, maximum, or bounded experience range overrides every selected preset.
- A candidate without reliable numeric years of experience receives an `unknown` seniority-range evaluation. A title-derived seniority label is not a substitute.

## 3. Scope

### 3.1 Included

- Authenticated PDF and DOCX text extraction
- File size, type, structure, and text validation
- Clear errors for corrupted, encrypted, empty, image-only, unsupported, oversized, overlong, or overly complex documents
- Editable review of extracted text before scorecard generation
- Typed seniority values and visible preset definitions
- Multiple preset selection
- An explicit custom-range override with open-ended bounds
- Centralized resolution of preset selections to numeric intervals
- Numeric, evidence-based seniority matching
- Compatibility handling for historical scorecards and older drafts
- A new matching-model version for newly scored candidates

### 3.2 Excluded

- OCR
- Retention, preview, or download of the uploaded job-description file
- Candidate resume upload, storage, preview, or download
- Automatic extraction of candidate experience from a resume
- Candidate-detail layout or tag changes
- Job-title, location, or active-scorecard editing
- Rescoring candidates already stored for an active scorecard
- Background document processing
- Changes to the existing 50,000-character job-description limit

## 4. Existing System Context

The application is a modular monolith with a Next.js web application, a FastAPI backend, and a jobs/scorecards domain module. The current intake form accepts pasted text and then calls the existing create-job and generate-scorecard APIs. Scorecard versions already store a `seniority` JSON list and nullable `minimum_years` and `maximum_years` columns. Confirmed scorecards are immutable.

The current matching engine evaluates provider seniority labels separately from numeric years. That permits title- or provider-derived labels to influence seniority matching and does not give preset names one stable numeric definition. This design replaces that behavior for new matching results.

## 5. Architecture

### 5.1 Document extraction boundary

Document extraction belongs to the jobs/scorecards domain because its only output is job-description text used by job intake. It is exposed behind a `JobDescriptionExtractor` interface with deterministic PDF and DOCX implementations. The language-model gateway does not parse files.

The extraction service performs these steps in order:

1. Enforce the request size limit before parsing.
2. Compare the declared media type, filename extension, and file signature.
3. Route the file to the PDF or DOCX parser.
4. Reject encrypted, corrupt, structurally unsafe, or overly complex content.
5. Extract text without OCR or external network access.
6. Normalize the text without rewriting its meaning.
7. Validate that readable text exists and that the result is no more than 50,000 Unicode code points.
8. Return the text and transient display metadata.
9. Dispose of the request buffer or request-scoped temporary file.

Normalization converts line endings to `\n`, removes NUL and non-printing control characters other than tab and newline, trims leading and trailing whitespace, and preserves paragraph separation. It does not summarize, reorder, translate, or otherwise rewrite content.

### 5.2 API and BFF

Add an authenticated endpoint:

```http
POST /api/v1/job-descriptions/extract
Content-Type: multipart/form-data
```

The multipart request contains exactly one `file` part. A successful response is:

```json
{
  "text": "Extracted job-description text",
  "source": {
    "filename": "product-designer.docx",
    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
  }
}
```

`source` exists only for the current form interaction. Neither it nor the file is added to the job record. The Next.js application exposes a corresponding `/api/bff/job-descriptions/extract` route and applies its existing authentication, bounded-response, and error-mapping conventions.

The extraction endpoint requires a valid tenant membership but no client identifier because it persists nothing and returns the caller's own submitted content. File contents and extracted text must not be written to logs, traces, analytics, or error payloads.

### 5.3 Seniority policy boundary

Add a single seniority policy component in the jobs/scorecards domain. Validation, display serialization, scorecard generation, and matching depend on this policy rather than duplicating range constants.

The API values are:

```text
early_career
mid_level
senior
```

Their inclusive numeric intervals are:

| Preset | Display label | Effective interval |
| --- | --- | --- |
| `early_career` | Early-Career | `[0, 3]` |
| `mid_level` | Mid-Level | `[3, 9]` |
| `senior` | Senior | `[10, infinity]` |

The policy returns an ordered set of effective intervals. It never reduces disjoint selections to their minimum and maximum envelope. For example, selecting Early-Career and Senior yields `[0, 3]` or `[10, infinity]`; it does not match 4–9 years.

## 6. Recruiter Flow

1. The recruiter selects an authorized client and supplies the existing job details.
2. The recruiter either pastes a job description or chooses one PDF or DOCX file.
3. File selection calls the extraction BFF without creating a job.
4. While extraction is running, the upload control and scorecard-generation action are disabled and an accessible progress status is shown.
5. If the text area already contains non-whitespace content, the UI asks for confirmation before replacing it. Cancelling leaves the existing text untouched and does not upload the file.
6. Successful extraction replaces the text area with the returned text and identifies the current content as extracted from the selected filename.
7. The recruiter edits the text as needed.
8. The recruiter selects zero or more seniority presets in the generated scorecard draft.
9. The recruiter may enable “Use custom experience range” and enter a minimum, a maximum, or both.
10. The recruiter confirms the scorecard through the existing draft and confirmation flow.

Upload and paste are alternative ways to populate one field. Once text is in the field, the system does not retain a distinction that affects scorecard generation.

## 7. Seniority Data and Validation

### 7.1 Scorecard representation

The existing scorecard fields remain:

- `seniority`: a unique list containing zero to three typed preset values
- `minimum_years`: a nullable integer from 0 through 50
- `maximum_years`: a nullable integer from 0 through 50

No new database column is required. The custom override is active when at least one numeric bound is non-null. It is inactive when both bounds are null. The frontend control may be temporarily enabled with empty inputs while the recruiter is editing, but such a draft cannot be saved or confirmed.

If both bounds exist, `minimum_years` must be less than or equal to `maximum_years`. Bounds are inclusive. The supported custom shapes are:

- minimum only: `[minimum, infinity]`
- maximum only: `[0, maximum]`
- minimum and maximum: `[minimum, maximum]`

### 7.2 Override semantics

When a custom override is active, the selected presets remain stored and visible but are inactive for matching. The UI disables their controls and displays: “This custom range overrides the selected seniority levels.” Turning the override off clears both custom bounds and restores the stored preset union.

The backend determines precedence from the presence of a custom bound. It never trusts a frontend-only flag. This makes API, worker, and test behavior identical.

### 7.3 Generated drafts

The language-model scorecard schema emits only canonical preset values. Generic descriptions such as “senior designer” may select a preset. Explicit numeric requirements such as “five to eight years” populate the custom bounds. When both a preset label and explicit numeric bounds are present, the draft retains both and the numeric bounds are the active override. The recruiter can remove or change either before confirmation.

The gateway must not invent a numeric bound that is absent from the source without marking it as an inferred item under the existing confirmation rules.

## 8. Matching Semantics

### 8.1 Matching evaluation

Matching uses `candidate.years_experience` as the only evidence for the seniority-range requirement.

- With active custom bounds, a candidate is supported when the numeric value falls within the custom interval.
- Without custom bounds, a candidate is supported when the numeric value falls within at least one selected preset interval.
- A known value outside every effective interval is failed.
- A missing value is unknown when an effective requirement exists.
- With no custom bounds and no selected presets, no seniority-range criterion is produced.
- `candidate.seniority`, current-title prefixes, and experience-title prefixes do not prove the seniority-range criterion.

Other title, skill, career-trajectory, and recency evaluations remain unchanged. If no seniority requirement exists and there is no other scope requirement, the scope/seniority/years component contributes zero rather than manufacturing evidence or renormalizing the score.

Because this changes score interpretation, new results use matching-model version `matching-v2`. Historical candidate results retain their stored model version, score, breakdown, and evidence. This slice does not rescore them.

### 8.2 Provider query planning

Provider seniority filters are sourcing hints, not match evidence. When no custom override is active, the query planner maps the canonical presets to connector-specific seniority filters and plans the union of selected presets. When a custom override is active, the planner must not send the inactive preset selection as a provider seniority filter because doing so could exclude candidates who satisfy the custom range. It instead sources with the remaining confirmed criteria and applies the custom numeric interval during deterministic matching.

Connector responses may still populate `candidate.seniority` for display or other non-range uses, but that field cannot support or fail the seniority-range criterion.

## 9. File Validation and Error Contract

The backend owns authoritative validation. Frontend checks improve feedback but do not replace server checks.

| Condition | HTTP status | Stable code | Recruiter message |
| --- | ---: | --- | --- |
| Missing file or more than one file | 400 | `job_description_file_required` | “Choose one PDF or DOCX job description.” |
| File exceeds 10,000,000 bytes | 413 | `job_description_file_too_large` | “The job description file must be 10 MB or smaller.” |
| Unsupported extension, media type, or signature | 415 | `job_description_type_unsupported` | “Upload a PDF or DOCX job description.” |
| Corrupt or encrypted document | 422 | `job_description_file_unreadable` | “The uploaded job description file is corrupted or might be password-protected.” |
| Empty or image-only document | 422 | `job_description_text_missing` | “No readable text was found. Upload a text-based document or paste the job description.” |
| Extracted text exceeds 50,000 characters | 422 | `job_description_text_too_long` | “The extracted job description is too long. Paste a shortened version of 50,000 characters or fewer.” |
| Parser safety ceiling exceeded | 422 | `job_description_file_too_complex` | “The job description could not be processed safely. Upload a simpler file or paste the text.” |
| Unexpected extraction service failure or timeout | 503 | `job_description_extraction_unavailable` | “The job description could not be extracted. Try again safely or paste the text.” |

Corrupt and password-protected files intentionally share one code and message. Parser-specific exception text is never returned to the browser.

Initial parser safety ceilings are:

- no more than 200 PDF pages
- no more than 2,000 DOCX archive entries
- no more than 50,000,000 expanded DOCX bytes
- no nested archives
- a 10-second extraction deadline per request

Crossing any structural ceiling produces `job_description_file_too_complex`. Crossing the deadline produces `job_description_extraction_unavailable`.

## 10. Security and Privacy

- Verify file signature as well as extension and declared media type.
- Accept `.pdf` and `.docx` only; reject macro-enabled Office formats.
- Configure PDF parsing to avoid external resource access.
- Parse DOCX as a bounded ZIP package and never follow external relationships.
- Use request-scoped memory or a request-scoped spooled temporary file. Delete or close it in a `finally` path on success, validation failure, parser failure, timeout, and client disconnect.
- Never pass uploaded bytes to the language model.
- Never include file bytes, extracted content, or source fragments in application logs or telemetry.
- Keep filenames out of metrics and structured logs.
- Record only non-sensitive operational dimensions such as format, byte-size bucket, result code, and duration bucket.

## 11. Compatibility and Versioning

Confirmed scorecard versions are immutable and remain readable exactly as stored. Existing candidate results are not recalculated.

New drafts and newly confirmed scorecards accept only canonical seniority values. When an older editable draft is loaded:

- exact, case-insensitive aliases for `early_career`, `early-career`, `mid_level`, `mid-level`, and `senior` normalize to canonical values;
- duplicates are removed while preserving canonical display order;
- any other value is shown as invalid and must be corrected before the draft can be saved or confirmed.

Unknown historical values in an already confirmed scorecard remain displayable as historical data. They are not silently reclassified. Starting a new run from such a scorecard requires an explicit revised, canonical scorecard version.

The frontend schemas, generated API types, backend schemas, scorecard gateway contract, and matching fixtures must change atomically so typed values cannot drift between layers.

## 12. Testing Strategy

### 12.1 Extraction unit tests

- Readable text-based PDF
- Readable DOCX with headings, paragraphs, tables, and lists
- Encrypted PDF
- Corrupt PDF and DOCX
- Image-only and empty PDF
- DOCX with no readable text
- Mismatched extension, media type, and signature
- File sizes immediately below, at, and above 10,000,000 bytes
- Text lengths immediately below, at, and above 50,000 characters
- PDF page ceiling
- DOCX entry and expanded-size ceilings
- External DOCX relationships are not fetched
- Normalization preserves paragraph order and removes prohibited control characters
- Cleanup occurs on success, every expected failure, timeout, and client disconnect

### 12.2 Seniority unit and property tests

- Preset boundaries at 0, 3, 4, 9, and 10 years
- Three years matches both Early-Career and Mid-Level
- Each individual preset
- Every multi-preset combination
- Disjoint Early-Career plus Senior selection does not match 4–9 years
- Minimum-only, maximum-only, and bounded custom ranges
- Inclusive custom endpoints
- Custom bounds override all selected presets
- Empty selection with no custom override produces no criterion
- Missing numeric candidate experience produces unknown when a requirement exists
- Candidate or title seniority labels never convert unknown numeric experience to supported
- Invalid, duplicate, and unknown preset values are rejected or normalized according to the compatibility rules
- Provider query planning maps active presets but omits inactive presets when a custom override exists

### 12.3 API integration tests

- Authentication and valid tenant membership are required
- Multipart requests contain exactly one supported file
- Stable status codes and error codes map to the documented conditions
- Uploaded bytes and transient metadata are never written to job or scorecard tables
- Reviewed extracted text, including recruiter edits, is the exact text persisted on job creation
- Canonical presets and custom bounds survive draft update and confirmation
- Existing optimistic revision checks remain effective

### 12.4 Web tests

- Paste-only intake remains unchanged
- File upload populates the editable text area
- Existing text requires confirmation before replacement
- Cancelling replacement preserves existing text and avoids upload
- Extraction progress and errors are accessible and focus-managed
- Generation is disabled during extraction
- Multiple presets can be selected
- No preset is valid
- Enabling the custom override reveals the fields and requires at least one bound
- Active custom bounds visibly override and disable preset matching controls
- Turning the override off clears custom bounds and restores preset behavior

### 12.5 End-to-end and regression tests

- A PDF produces editable text and a reviewable scorecard
- A DOCX produces editable text and a reviewable scorecard
- Corrupt and encrypted files display the required shared error
- Image-only PDFs invite paste fallback and do not start OCR
- The original upload is unavailable after extraction and is absent from persistent storage
- Pasted descriptions still create scorecards
- Historical scorecards and candidate results retain their prior values and model versions
- New runs use the new seniority policy and matching-model version
- Custom overrides cannot be narrowed accidentally by stale provider seniority filters

## 13. Success Criteria

The slice is complete when:

- Recruiters can populate the job-description field from a readable PDF or DOCX of at most 10 MB.
- Recruiters can review and edit extracted text before any job or scorecard is created.
- Uploaded files and transient metadata are not persisted.
- Every documented failure produces its stable code and recruiter-facing message.
- Seniority presets display their approved definitions and support multiple or zero selections.
- Three years matches both Early-Career and Mid-Level.
- Valid open-ended or bounded custom ranges override all presets.
- Missing numeric candidate experience is unknown rather than inferred from a title.
- Historical scorecards and candidate results remain unchanged.
- All tests in Section 12 pass.

## 14. Follow-On Work

After this slice is implemented and verified, the remaining parent request should proceed through separate design cycles:

1. Active-scorecard editing and conditional candidate rescoring
2. Candidate contact and resume placement, storage, preview, and download
3. Evidence-based candidate tags and removal of internal labels from recruiter-facing UI
