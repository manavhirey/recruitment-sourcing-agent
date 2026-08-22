"use client"

import { useRef, useState } from "react"
import { useRouter } from "next/navigation"

import {
  ClientResponseError,
  reauthenticateExpiredSession,
  requireResponse,
} from "@/lib/client-response"

export function GenerateScorecardAction({
  jobId,
  expectedRevision,
  onGenerated,
}: {
  jobId: string
  expectedRevision: number
  onGenerated?: () => void
}) {
  const router = useRouter()
  const intentKey = useRef<string | null>(null)
  const inFlight = useRef(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function generate() {
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    setError(null)
    intentKey.current ??= crypto.randomUUID()
    try {
      const response = await fetch(`/api/bff/jobs/${jobId}/scorecard/generate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": intentKey.current,
        },
        body: JSON.stringify({ expected_revision: expectedRevision }),
      })
      try {
        await requireResponse(response)
      } catch (error) {
        if (
          error instanceof ClientResponseError &&
          error.code === "scorecard_revision_conflict"
        ) {
          if (onGenerated) onGenerated()
          else router.refresh()
          return
        }
        throw error
      }
      if (onGenerated) onGenerated()
      else router.refresh()
    } catch (error) {
      if (reauthenticateExpiredSession(error, router)) return
      setError("The scorecard was not generated. Retry uses the same safe request.")
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }

  return (
    <div className="empty-state">
      <p className="eyebrow">Job saved</p>
      <h2>Generate the scorecard to continue.</h2>
      <p>This resumes the existing job and will not create a duplicate intake.</p>
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      <button
        className="button button-primary"
        type="button"
        disabled={submitting}
        onClick={generate}
      >
        {submitting ? "Generating…" : "Generate scorecard"}
      </button>
    </div>
  )
}
