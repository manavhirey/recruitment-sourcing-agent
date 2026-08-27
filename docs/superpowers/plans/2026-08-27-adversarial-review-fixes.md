# Adversarial Review Fixes Implementation Plan

> **For Codex:** Use `superpowers:subagent-driven-development` to execute this plan in the existing isolated feature worktree.

**Goal:** Close the four verified PR #4 review findings so numeric seniority works on production candidate data, generated experience bounds fail closed, extraction cannot overwrite newly entered text, and expired extraction sessions reauthenticate consistently.

**Architecture:** Keep numeric seniority evidence derived only from structured numeric dates—never titles or provider seniority labels—and make the derivation deterministic for the same persisted candidate data. Harden the LLM gateway at its trust boundary by adding confirmation-required uncertainty for every generated numeric bound that is not already marked. Keep the upload fix local to the existing intake/upload components and reuse the shared client reauthentication behavior.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Pydantic, pytest; TypeScript, React, Next.js, Vitest/Testing Library.

**Binding constraints:**

- Work only in `/Users/MAC/Documents/ChatGPT/Recuritment sourcing/.worktrees/scorecard-creation-seniority-implementation` on `codex/scorecard-creation-seniority-implementation`.
- Follow strict TDD for every finding: add the smallest real-behavior regression, run it and capture the expected failure, implement the minimum fix, rerun it green.
- Do not derive years from titles, provider seniority labels, or undated experience entries.
- Numeric experience derived from employment intervals must not double-count overlapping roles and must use a stable date already present in persisted source data for open-ended `present/current/now` roles; if reliable dates are insufficient, keep the result `unknown`.
- Do not add a database migration unless inspection proves a deterministic derived profile value cannot satisfy the production path safely.
- Model-generated numeric bounds must never become active without recruiter confirmation merely because they pass the Pydantic schema. Preserve existing confirmation IDs and API shapes.
- Existing text must never be silently replaced by an extraction or retry response. A retry that would replace non-whitespace text must obtain confirmation.
- A 401 extraction response must use `reauthenticateExpiredSession`; document parsing errors retain their existing messages.
- Preserve the existing 10 MB/no-OCR/no-retention extraction contract, tenant isolation, idempotency behavior, and public error redaction.
- Do not push, merge, alter the PR, or modify unrelated code. Commit the implementation locally.

## Task 1: Address all verified adversarial-review findings

**Primary files:**

- Modify: `backend/app/candidates/service.py`
- Modify only if needed for a focused helper/interface: `backend/app/candidates/schemas.py`, `backend/app/matching/engine.py`
- Modify: `backend/app/jobs/llm.py`
- Modify: `web/components/jobs/JobDescriptionUpload.tsx`
- Modify: `web/components/jobs/JobIntakeForm.tsx`
- Tests: production-path candidate integration/service tests, `backend/tests/unit/jobs/test_scorecard_gateway.py`, `web/tests/jobs/job-description-upload.test.tsx`, and `web/tests/jobs/job-intake.test.tsx`

### 1. Production numeric-experience evidence

Add a regression that starts with realistic `ProviderPerson` employment-history dates, persists the candidate through `CandidateService`, reloads it with `get_profile()`, and evaluates a canonical preset/custom range. Prove RED because `years_experience` is currently `None`. Implement a deterministic duration calculation over the union of valid employment intervals, avoiding overlap double-counting. Treat partial or unparseable dates conservatively; do not infer from titles. Use a persisted source observation date as the end for open roles so repeated reads are stable. Prove GREEN and retain an explicit unknown case when reliable dated evidence is absent.

### 2. Fail-closed generated numeric bounds

Add a gateway regression where the model returns one or both numeric bounds for a description with no numeric requirement and no bound-specific uncertainty. Prove RED because the draft is currently accepted without unresolved confirmation. At the LLM gateway boundary, ensure each returned numeric bound has its exact confirmation-required uncertainty (`Confirm inferred minimum years: N` / `Confirm inferred maximum years: N`) unless already present, without duplicating entries. Do not change manually edited draft behavior. Prove GREEN, including one-sided and bounded ranges.

### 3. Prevent extraction overwrite races

Add deferred-response UI regressions for both initial extraction and retry: text entered or changed after the original intent must not be silently overwritten. Prove RED. Make the textarea non-editable while extraction is actively in flight and require the existing replacement confirmation before retrying when the current text is non-whitespace. Keep extracted text editable immediately after completion. Prove GREEN and preserve the existing cancellation behavior.

### 4. Reauthenticate expired extraction sessions

Add a UI regression for an extraction 401. Prove RED because it currently displays the generic extraction error/retry. Route the error through `reauthenticateExpiredSession` and preserve the callback URL behavior used elsewhere. Prove GREEN; non-401 extraction errors must retain their current accessible messaging.

### 5. Verification and commit

Run the focused backend and web tests for all four findings, then the affected backend candidate/matching/job suites and affected web upload/intake suites. Run Ruff check/format, mypy, web lint/typecheck, and `git diff --check`. If feasible in the available time, run the complete backend and web unit suites once. Self-review the full diff for scope, deterministic behavior, and real production-path coverage. Commit locally with a concise `fix:` subject. Do not push.
