# Document Extraction Boundary Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two security findings that keep PR #4 in draft: multipart preambles bypassing BFF part-header limits and PDF Form XObject streams bypassing the decoded-byte limit.

**Architecture:** Treat the browser-facing multipart ingress as a strict profile: extract the declared boundary, require its opening delimiter at byte zero, and measure only the actual first part header block. For PDFs, walk every reachable Form XObject resource iteratively, deduplicate stream objects, and add their decoded bytes to the existing document-wide budget before text extraction.

**Tech Stack:** TypeScript, Next.js BFF, `@fastify/busboy`, Vitest, Python 3.12, `pypdf`, pytest.

**Spec:** [PR #4](https://github.com/manavhirey/recruitment-sourcing-agent/pull/4), especially its “Known blockers before merge” section.

## Global Constraints

- Preserve the exact public error codes: malformed multipart is `job_description_file_required`; excessive body/file size is `job_description_file_too_large`; excessive decoded PDF complexity is `job_description_file_too_complex`.
- Preserve the 10,000,000-byte file limit, 8,192-byte multipart part-header limit, 8-header-pair limit, and 50,000,000-byte aggregate decoded-PDF limit.
- Authentication and tenant resolution must still occur before any request-body read.
- Do not add OCR or persist uploaded source documents.
- Use strict TDD: add each regression test first, run it against the vulnerable implementation, record the expected failure, then write production code.
- Keep changes scoped to the two findings and their tests; do not refactor unrelated extraction behavior.

---

### Task 1: Close both extraction-boundary bypasses

**Files:**
- Modify: `web/lib/multipart-upload.ts`
- Test: `web/tests/security/document-extraction-bff.test.ts`
- Modify: `backend/app/jobs/document_extraction.py`
- Modify: `backend/tests/job_description_fixtures.py`
- Test: `backend/tests/unit/jobs/test_document_extraction.py`

**Interfaces:**
- Consumes: `readMultipartUpload(request: Request): Promise<MultipartUpload>` and `DefaultJobDescriptionExtractor.extract(...)`.
- Produces: unchanged public interfaces and unchanged public error codes; only malicious/over-complex inputs change from acceptance to rejection.

- [ ] **Step 1: Add the multipart preamble regression test**

Add a raw multipart test that places `\r\n\r\n` in a preamble before the declared opening boundary, then supplies an actual first part whose headers exceed `maximumHeaderBytes`. Assert a `400` response with `job_description_file_required` and assert that `callApi` was not called.

```ts
it("rejects a preamble that could hide oversized part headers", async () => {
  const boundary = "preamble-header-boundary"
  const consumed: number[] = []
  const callApi = vi.fn()
  const response = await handleDocumentExtraction(
    rawUploadRequest([
      textEncoder.encode("accepted preamble\r\n\r\n"),
      rawFileOpening(boundary, [`X-Oversized: ${"x".repeat(8_193)}`]),
      textEncoder.encode("%PDF-1.4"),
      textEncoder.encode(`\r\n--${boundary}--\r\n`),
    ], consumed, { boundary }),
    { appUrl, readTenant: async () => tenantId, callApi },
  )

  await expectError(response, 400, "job_description_file_required")
  expect(callApi).not.toHaveBeenCalled()
})
```

- [ ] **Step 2: Run the multipart test and verify RED**

Run:

```bash
cd web
npm test -- --run tests/security/document-extraction-bff.test.ts -t "preamble"
```

Expected: FAIL because the current probe treats the preamble’s first blank line as the end of the actual part headers and allows Busboy to reach the file.

- [ ] **Step 3: Make the multipart probe boundary-aware**

Add a private boundary parser that accepts quoted and unquoted boundary parameters, rejects missing/empty/CRLF/over-70-byte values, and returns the exact boundary Busboy is expected to parse.

```ts
function multipartBoundary(contentType: string): string | null {
  const match = /(?:^|;)\s*boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(contentType)
  const boundary = match?.[1] ?? match?.[2]
  if (
    !boundary ||
    Buffer.byteLength(boundary, "latin1") > 70 ||
    boundary.includes("\r") ||
    boundary.includes("\n")
  ) return null
  return boundary
}
```

Before constructing Busboy, require a valid boundary. Build `openingDelimiter = Buffer.from(`--${boundary}\r\n`, "latin1")`. While probing, require `headerProbe` to remain a prefix of that delimiter and, once enough bytes are present, require the request body to start with the complete delimiter. Reject preambles rather than scanning past them. Search for `\r\n\r\n` only from `openingDelimiter.byteLength`; compute byte and pair limits over that exact header slice. Preserve chunk-boundary handling and the existing bounded probe size.

- [ ] **Step 4: Verify multipart GREEN and surrounding security coverage**

Run:

```bash
cd web
npm test -- --run tests/security/document-extraction-bff.test.ts
npm run typecheck
npm run lint
```

Expected: all commands exit zero, including the new preamble regression and existing exact header/file/body boundary tests.

- [ ] **Step 5: Add Form XObject PDF fixtures and RED tests**

In `backend/tests/job_description_fixtures.py`, create a PDF fixture whose small page `/Contents` invokes a reachable `/Subtype /Form` XObject with a Flate-compressed decoded stream. Support a nested chain so the test proves traversal is recursive, not only one level deep.

Add tests using `monkeypatch` to lower `MAX_PDF_DECODED_BYTES` to a small hand-checkable value. One test must put the excess bytes in a directly referenced Form XObject; another must put them in a nested Form XObject. Both must assert `job_description_file_too_complex`.

```python
def test_rejects_pdf_when_form_xobject_exceeds_decoded_budget(
    extractor: DefaultJobDescriptionExtractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_extraction, "MAX_PDF_DECODED_BYTES", 100)
    assert_extraction_error(
        extractor,
        data=pdf_with_form_xobject_decoded_sizes((101,)),
        filename="role.pdf",
        media_type=PDF_MEDIA_TYPE,
        code="job_description_file_too_complex",
    )
```

The nested test uses a small outer Form that invokes an inner Form whose aggregate decoded bytes push the document over the same 100-byte budget.

- [ ] **Step 6: Run the Form XObject tests and verify RED**

Run:

```bash
cd backend
.venv/bin/pytest tests/unit/jobs/test_document_extraction.py -k "form_xobject" -q
```

Expected: FAIL because `_pdf_text` currently counts only each page’s direct `/Contents` stream.

- [ ] **Step 7: Count reachable Form XObject streams safely**

Add a private iterative helper used by `_pdf_text` before `extract_text()`:

```python
def _pdf_decoded_stream_bytes(page: PageObject) -> int:
    decoded_bytes = 0
    pending_resources = [page.get("/Resources")]
    seen_streams: set[tuple[int, int] | int] = set()
    # Resolve indirect objects, walk /XObject dictionaries, count only
    # /Subtype /Form stream get_data() lengths, and enqueue each Form’s
    # own /Resources. Use indirect object number/generation when present,
    # otherwise object identity, so shared resources and cycles terminate.
    return decoded_bytes
```

Keep the existing direct page-content accounting, add the helper result to the same document-wide `decoded_bytes`, and reject immediately once it exceeds `MAX_PDF_DECODED_BYTES`. Convert malformed resource structures and stream decode failures into the existing unreadable/complex public error contract rather than leaking library exceptions.

- [ ] **Step 8: Verify PDF GREEN and backend quality gates**

Run:

```bash
cd backend
.venv/bin/pytest tests/unit/jobs/test_document_extraction.py -q
.venv/bin/ruff check app/jobs/document_extraction.py tests/job_description_fixtures.py tests/unit/jobs/test_document_extraction.py
.venv/bin/ruff format --check app/jobs/document_extraction.py tests/job_description_fixtures.py tests/unit/jobs/test_document_extraction.py
.venv/bin/mypy app/jobs/document_extraction.py
```

Expected: all commands exit zero.

- [ ] **Step 9: Run final focused regression suites and commit**

Run:

```bash
cd web
npm test -- --run tests/security/document-extraction-bff.test.ts
npm run typecheck
npm run lint
cd ../backend
.venv/bin/pytest tests/unit/jobs/test_document_extraction.py -q
```

Review `git diff --check` and the complete diff, then commit only the scoped source, tests, fixture, and plan/report artifacts:

```bash
git add web/lib/multipart-upload.ts \
  web/tests/security/document-extraction-bff.test.ts \
  backend/app/jobs/document_extraction.py \
  backend/tests/job_description_fixtures.py \
  backend/tests/unit/jobs/test_document_extraction.py \
  docs/superpowers/plans/2026-08-25-document-extraction-boundary-fixes.md
git commit -m "fix: close document extraction boundary bypasses"
```
