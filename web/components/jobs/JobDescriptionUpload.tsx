"use client"

import { useEffect, useRef, useState, type ChangeEvent } from "react"

import { ModalDialog } from "@/components/layout/ModalDialog"
import { responseJson } from "@/lib/client-response"
import type { JobDescriptionExtraction } from "@/lib/schemas"

const maximumFileBytes = 10_000_000

const supportedTypes = new Map([
  [".pdf", "application/pdf"],
  [".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
])

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

type ExtractionIntent = {
  file: File
  key: string
}

export type JobDescriptionUploadProps = {
  currentText: string
  disabled: boolean
  onBusyChange: (busy: boolean) => void
  onExtracted: (result: JobDescriptionExtraction) => void
}

function inferredMediaType(file: File): string | null {
  const name = file.name.toLowerCase()
  return [...supportedTypes].find(([extension]) => name.endsWith(extension))?.[1] ?? null
}

function canonicalFile(file: File, mediaType: string): File {
  return new File([file], file.name, {
    type: mediaType,
    lastModified: file.lastModified,
  })
}

export function JobDescriptionUpload({
  currentText,
  disabled,
  onBusyChange,
  onExtracted,
}: JobDescriptionUploadProps) {
  const [extracting, setExtracting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [canRetry, setCanRetry] = useState(false)
  const intent = useRef<ExtractionIntent | null>(null)
  const errorAlert = useRef<HTMLParagraphElement>(null)
  const replaceButton = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!error) return
    const timeout = window.setTimeout(() => errorAlert.current?.focus())
    return () => window.clearTimeout(timeout)
  }, [error])

  function showError(code: string) {
    setError(extractionMessages[code] ?? extractionMessages.job_description_extraction_unavailable)
  }

  async function extract() {
    const current = intent.current
    if (!current || extracting) return
    setError(null)
    setExtracting(true)
    onBusyChange(true)
    try {
      const form = new FormData()
      form.set("file", current.file, current.file.name)
      const result = await responseJson<JobDescriptionExtraction>(
        await fetch("/api/bff/job-descriptions/extract", {
          method: "POST",
          headers: { "Idempotency-Key": current.key },
          body: form,
        }),
      )
      onExtracted(result)
      intent.current = null
      setCanRetry(false)
    } catch (reason) {
      showError(reason instanceof Error ? reason.message : "job_description_extraction_unavailable")
    } finally {
      setExtracting(false)
      onBusyChange(false)
    }
  }

  function clearIntent() {
    intent.current = null
    setCanRetry(false)
    setConfirming(false)
  }

  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0]
    event.currentTarget.value = ""
    if (!file) return
    setError(null)
    const mediaType = inferredMediaType(file)
    if (!mediaType) {
      intent.current = null
      setCanRetry(false)
      showError("job_description_type_unsupported")
      return
    }
    if (file.size > maximumFileBytes) {
      intent.current = null
      setCanRetry(false)
      showError("job_description_file_too_large")
      return
    }
    intent.current = { file: canonicalFile(file, mediaType), key: crypto.randomUUID() }
    setCanRetry(true)
    if (currentText.trim()) {
      setConfirming(true)
      return
    }
    void extract()
  }

  return (
    <div className="job-description-upload">
      <label htmlFor="job-description-upload">Upload job description</label>
      <input
        id="job-description-upload"
        type="file"
        accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        disabled={disabled || extracting}
        onChange={selectFile}
      />
      <p className="field-hint">Upload one PDF or DOCX to extract, then review and edit the text below.</p>
      {extracting ? <p className="upload-status" role="status">Extracting job description…</p> : null}
      {error ? (
        <div className="upload-error">
          <p ref={errorAlert} className="field-error" role="alert" tabIndex={-1}>{error}</p>
          {canRetry ? <button className="button button-secondary" type="button" onClick={() => void extract()}>Try again</button> : null}
        </div>
      ) : null}
      {confirming ? (
        <ModalDialog
          labelledBy="replace-job-description-heading"
          initialFocus={replaceButton}
          onClose={clearIntent}
        >
          <h3 id="replace-job-description-heading">Replace job description?</h3>
          <p>The uploaded document will replace the current job-description text. You can still edit the extracted text before generating a scorecard.</p>
          <div className="dialog-actions">
            <button className="button button-secondary" type="button" onClick={clearIntent}>Keep existing text</button>
            <button
              ref={replaceButton}
              className="button button-primary"
              type="button"
              onClick={() => {
                setConfirming(false)
                void extract()
              }}
            >
              Replace text
            </button>
          </div>
        </ModalDialog>
      ) : null}
    </div>
  )
}
