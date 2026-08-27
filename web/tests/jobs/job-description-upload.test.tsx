import { act, fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { useState } from "react"
import { vi } from "vitest"

import { JobDescriptionUpload } from "@/components/jobs/JobDescriptionUpload"
import { server } from "@/tests/setup"

function UploadHarness({
  initialText = "",
  disabled = false,
}: {
  initialText?: string
  disabled?: boolean
}) {
  const [text, setText] = useState(initialText)
  const [busy, setBusy] = useState(false)
  const [source, setSource] = useState<string | null>(null)
  return (
    <>
      <JobDescriptionUpload
        currentText={text}
        disabled={disabled}
        onBusyChange={setBusy}
        onExtracted={(result) => {
          setText(result.text)
          setSource(result.source.filename)
        }}
      />
      <label htmlFor="reviewed-job-description">Job description</label>
      <textarea id="reviewed-job-description" value={text} onChange={(event) => setText(event.target.value)} />
      <span aria-label="Extraction busy">{String(busy)}</span>
      {source ? <p>Extracted from {source}</p> : null}
    </>
  )
}

describe("JobDescriptionUpload", () => {
  it.each([
    ["role.pdf", "", "application/pdf"],
    ["role.docx", "application/octet-stream", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
  ])("accepts %s with a generic MIME type and normalizes it for the BFF", async (filename, type, mediaType) => {
    server.use(http.post("/api/bff/job-descriptions/extract", async ({ request }) => {
      expect(await request.text()).toContain(`Content-Type: ${mediaType}`)
      return HttpResponse.json({
        text: "Extracted text",
        source: { filename, media_type: mediaType },
      })
    }))
    const user = userEvent.setup({ applyAccept: false })
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["document"], filename, { type }),
    )

    expect(await screen.findByText(`Extracted from ${filename}`)).toBeVisible()
  })

  it("accepts a job description file at the exact 10 MB limit", async () => {
    const called = vi.fn(() => HttpResponse.json({
      text: "Extracted text",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }))
    server.use(http.post("/api/bff/job-descriptions/extract", called))
    const user = userEvent.setup()
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File([new Uint8Array(10_000_000)], "role.pdf", { type: "application/pdf" }),
    )

    expect(await screen.findByText("Extracted from role.pdf")).toBeVisible()
    expect(called).toHaveBeenCalledOnce()
  })

  it("rejects a job description file over 10 MB locally and focuses the alert", async () => {
    const called = vi.fn()
    server.use(http.post("/api/bff/job-descriptions/extract", called))
    const user = userEvent.setup()
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File([new Uint8Array(10_000_001)], "role.pdf", { type: "application/pdf" }),
    )

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("The job description file must be 10 MB or smaller.")
    await vi.waitFor(() => expect(alert).toHaveFocus())
    expect(called).not.toHaveBeenCalled()
  })

  it("guards programmatic file selection while submission is disabled", () => {
    const called = vi.fn()
    server.use(http.post("/api/bff/job-descriptions/extract", called))
    render(<UploadHarness disabled />)

    const input = screen.getByLabelText("Upload job description")
    input.removeAttribute("disabled")
    fireEvent.change(input, {
      target: {
        files: [
          new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
        ],
      },
    })

    expect(called).not.toHaveBeenCalled()
  })

  it("extracts a DOCX and leaves the returned text editable", async () => {
    server.use(http.post("/api/bff/job-descriptions/extract", () => {
      return HttpResponse.json({
        text: "Senior Product Designer\nLead growth design.",
        source: {
          filename: "role.docx",
          media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
      })
    }))
    const user = userEvent.setup()
    render(<UploadHarness />)
    const file = new File(["docx"], "role.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    })

    await user.upload(
      screen.getByLabelText("Upload job description"),
      file,
    )

    expect(await screen.findByLabelText("Job description")).toHaveValue(
      "Senior Product Designer\nLead growth design.",
    )
    await user.type(screen.getByLabelText("Job description"), " Edited")
    expect(screen.getByLabelText("Job description")).toHaveValue(
      "Senior Product Designer\nLead growth design. Edited",
    )
    expect(screen.getByText("Extracted from role.docx")).toBeVisible()
    await user.upload(screen.getByLabelText("Upload job description"), file)
    expect(screen.getByRole("dialog", { name: "Replace job description?" })).toBeVisible()
  })

  it("asks before replacing existing text and cancellation does not send the file", async () => {
    const called = vi.fn()
    server.use(http.post("/api/bff/job-descriptions/extract", called))
    const user = userEvent.setup()
    render(<UploadHarness initialText="Keep this text" />)
    const file = new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" })

    await user.upload(
      screen.getByLabelText("Upload job description"),
      file,
    )

    expect(screen.getByRole("dialog", { name: "Replace job description?" })).toBeVisible()
    await user.click(screen.getByRole("button", { name: "Keep existing text" }))
    expect(screen.getByLabelText("Job description")).toHaveValue("Keep this text")
    expect(called).not.toHaveBeenCalled()
    await user.upload(screen.getByLabelText("Upload job description"), file)
    expect(screen.getByRole("dialog", { name: "Replace job description?" })).toBeVisible()
  })

  it("disables every replacement-modal action while submission is active", async () => {
    const called = vi.fn()
    server.use(http.post("/api/bff/job-descriptions/extract", called))
    const user = userEvent.setup()
    const { rerender } = render(<UploadHarness initialText="Keep this text" />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    rerender(<UploadHarness initialText="Keep this text" disabled />)

    expect(screen.getByRole("button", { name: "Keep existing text" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Replace text" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "Replace text" }))
    expect(called).not.toHaveBeenCalled()
  })

  it("replaces existing text only after the recruiter confirms", async () => {
    server.use(http.post("/api/bff/job-descriptions/extract", () =>
      HttpResponse.json({
        text: "Replacement text",
        source: { filename: "role.pdf", media_type: "application/pdf" },
      }),
    ))
    const user = userEvent.setup()
    render(<UploadHarness initialText="Keep this text" />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    await user.click(screen.getByRole("button", { name: "Replace text" }))

    expect(await screen.findByLabelText("Job description")).toHaveValue("Replacement text")
  })

  it("shows progress and prevents a duplicate extraction while a document is processing", async () => {
    let resolveRequest: ((value: Response) => void) | undefined
    server.use(http.post("/api/bff/job-descriptions/extract", () => new Promise<Response>((resolve) => {
      resolveRequest = resolve
    })))
    const user = userEvent.setup()
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )

    expect(await screen.findByRole("status")).toHaveTextContent("Extracting job description…")
    expect(screen.getByLabelText("Extraction busy")).toHaveTextContent("true")
    expect(screen.getByLabelText("Upload job description")).toBeDisabled()
    resolveRequest?.(HttpResponse.json({
      text: "Extracted text",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }))
    expect(await screen.findByText("Extracted from role.pdf")).toBeVisible()
  })

  it("rejects unsupported files before sending them and focuses the alert", async () => {
    const called = vi.fn()
    server.use(http.post("/api/bff/job-descriptions/extract", called))
    const user = userEvent.setup({ applyAccept: false })
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["text"], "role.txt", { type: "text/plain" }),
    )

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("Upload a PDF or DOCX job description.")
    await vi.waitFor(() => expect(alert).toHaveFocus())
    expect(called).not.toHaveBeenCalled()
  })

  it.each([
    ["job_description_file_required", "Choose one PDF or DOCX job description."],
    ["job_description_file_too_large", "The job description file must be 10 MB or smaller."],
    ["job_description_type_unsupported", "Upload a PDF or DOCX job description."],
    ["job_description_file_unreadable", "The uploaded job description file is corrupted or might be password-protected."],
    ["job_description_text_missing", "No readable text was found. Upload a text-based document or paste the job description."],
    ["job_description_text_too_long", "The extracted job description is too long. Paste a shortened version of 50,000 characters or fewer."],
    ["job_description_file_too_complex", "The job description could not be processed safely. Upload a simpler file or paste the text."],
    ["job_description_extraction_unavailable", "The job description could not be extracted. Try again safely or paste the text."],
  ])("maps %s to its stable recruiter-facing message", async (code, message) => {
    server.use(http.post("/api/bff/job-descriptions/extract", () =>
      HttpResponse.json({ code }, { status: 422 }),
    ))
    const user = userEvent.setup()
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent(message)
    await vi.waitFor(() => expect(alert).toHaveFocus())
  })

  it("retries a failed file request with its original idempotency key", async () => {
    const keys: string[] = []
    let attempt = 0
    server.use(http.post("/api/bff/job-descriptions/extract", ({ request }) => {
      keys.push(request.headers.get("Idempotency-Key") ?? "")
      attempt += 1
      if (attempt === 1) return HttpResponse.json({ code: "job_description_extraction_unavailable" }, { status: 503 })
      return HttpResponse.json({
        text: "Extracted text",
        source: { filename: "role.pdf", media_type: "application/pdf" },
      })
    }))
    const user = userEvent.setup()
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    await user.click(await screen.findByRole("button", { name: "Try again" }))

    expect(await screen.findByText("Extracted from role.pdf")).toBeVisible()
    expect(keys).toHaveLength(2)
    expect(keys[0]).not.toBe("")
    expect(keys[0]).toBe(keys[1])
  })

  it("confirms before a deferred retry can replace text entered after the failure", async () => {
    let attempt = 0
    let resolveRetry: ((value: Response) => void) | undefined
    server.use(http.post("/api/bff/job-descriptions/extract", () => {
      attempt += 1
      if (attempt === 1) {
        return HttpResponse.json(
          { code: "job_description_extraction_unavailable" },
          { status: 503 },
        )
      }
      return new Promise<Response>((resolve) => {
        resolveRetry = resolve
      })
    }))
    const user = userEvent.setup()
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    await user.type(screen.getByLabelText("Job description"), "Keep this retry text")
    await user.click(await screen.findByRole("button", { name: "Try again" }))

    expect(screen.getByRole("dialog", { name: "Replace job description?" })).toBeVisible()
    expect(attempt).toBe(1)
    expect(screen.getByLabelText("Job description")).toHaveValue("Keep this retry text")

    await user.click(screen.getByRole("button", { name: "Replace text" }))
    await vi.waitFor(() => expect(attempt).toBe(2))
    expect(screen.getByLabelText("Extraction busy")).toHaveTextContent("true")
    resolveRetry?.(HttpResponse.json({
      text: "Extracted retry text",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }))
    expect(await screen.findByLabelText("Job description")).toHaveValue("Extracted retry text")
  })

  it("disables retry and guards it while submission is active", async () => {
    const called = vi.fn(() =>
      HttpResponse.json(
        { code: "job_description_extraction_unavailable" },
        { status: 503 },
      ),
    )
    server.use(http.post("/api/bff/job-descriptions/extract", called))
    const user = userEvent.setup()
    const { rerender } = render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    const retry = await screen.findByRole("button", { name: "Try again" })
    rerender(<UploadHarness disabled />)

    expect(retry).toBeDisabled()
    fireEvent.click(retry)
    expect(called).toHaveBeenCalledOnce()
  })

  it("guards a retry against two clicks in the same render", async () => {
    let attempt = 0
    let resolveRetry: ((value: Response) => void) | undefined
    server.use(http.post("/api/bff/job-descriptions/extract", () => {
      attempt += 1
      if (attempt === 1) {
        return HttpResponse.json(
          { code: "job_description_extraction_unavailable" },
          { status: 503 },
        )
      }
      return new Promise<Response>((resolve) => {
        resolveRetry = resolve
      })
    }))
    const user = userEvent.setup()
    render(<UploadHarness />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    const retry = await screen.findByRole("button", { name: "Try again" })
    act(() => {
      retry.dispatchEvent(new MouseEvent("click", { bubbles: true }))
      retry.dispatchEvent(new MouseEvent("click", { bubbles: true }))
    })

    await vi.waitFor(() => expect(attempt).toBe(2))
    resolveRetry?.(HttpResponse.json({
      text: "Extracted text",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }))
    expect(await screen.findByText("Extracted from role.pdf")).toBeVisible()
  })

  it("resets the native input so the same file can be selected after an error", async () => {
    let attempt = 0
    server.use(http.post("/api/bff/job-descriptions/extract", () => {
      attempt += 1
      return attempt === 1
        ? HttpResponse.json({ code: "job_description_text_missing" }, { status: 422 })
        : HttpResponse.json({
          text: "Extracted text",
          source: { filename: "role.pdf", media_type: "application/pdf" },
        })
    }))
    const user = userEvent.setup()
    render(<UploadHarness />)
    const file = new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" })
    const input = screen.getByLabelText("Upload job description")

    await user.upload(input, file)
    await screen.findByRole("alert")
    await user.upload(input, file)

    expect(await screen.findByText("Extracted from role.pdf")).toBeVisible()
    expect(attempt).toBe(2)
  })
})
