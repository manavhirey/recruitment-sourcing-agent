"use client"

import { useMutation } from "@tanstack/react-query"
import { useRouter } from "next/navigation"
import { useEffect, useRef, useState } from "react"

import { ContactReveal } from "@/components/candidates/ContactReveal"
import { ModalDialog } from "@/components/layout/ModalDialog"
import {
  ClientResponseError,
  reauthenticateExpiredSession,
  responseJson,
} from "@/lib/client-response"
import { matchEvidence } from "@/lib/review-data"
import type { JobCandidate } from "@/lib/schemas"

const rejectionReasons = [
  ["not_qualified", "Not qualified"],
  ["compensation_mismatch", "Compensation mismatch"],
  ["location_mismatch", "Location mismatch"],
  ["work_authorization", "Work authorization"],
  ["duplicate", "Duplicate"],
  ["other", "Other"],
] as const

type OwnerOption = { userId: string; name: string }
type Intent = {
  fingerprint: string
  key: string
  path: string
  method: "POST" | "PUT" | "PATCH"
  body: Record<string, unknown>
  optimistic: (candidate: JobCandidate) => JobCandidate
  successMessage: string
  afterSuccess?: () => void
}

export function CandidateDetail({
  candidate,
  ownerOptions = [],
  onRevalidate,
}: {
  candidate: JobCandidate
  ownerOptions?: readonly OwnerOption[]
  onRevalidate?: () => void
}) {
  const router = useRouter()
  const article = useRef<HTMLElement>(null)
  const errorBox = useRef<HTMLParagraphElement>(null)
  const rejectionReason = useRef<HTMLSelectElement>(null)
  const intentKeys = useRef(new Map<string, string>())
  const intentPending = useRef(false)
  const [optimisticView, setOptimisticView] = useState<{
    base: JobCandidate
    value: JobCandidate
  } | null>(null)
  const view = optimisticView?.base === candidate
    ? optimisticView.value
    : candidate
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState("")
  const [rejectNote, setRejectNote] = useState("")
  const [note, setNote] = useState("")
  const authoritativeTags = candidate.tags.join(", ")
  const [tagDraft, setTagDraft] = useState<{
    candidateId: string
    authoritative: string
    value: string
  } | null>(null)
  const tags = tagDraft?.candidateId === candidate.id &&
    tagDraft.authoritative === authoritativeTags
    ? tagDraft.value
    : authoritativeTags
  const [message, setMessage] = useState("")
  const [error, setError] = useState("")

  useEffect(() => {
    if (error) errorBox.current?.focus()
  }, [error])

  const mutation = useMutation({
    mutationFn: async (intent: Intent) => {
      const response = await fetch(intent.path, {
        method: intent.method,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": intent.key,
        },
        body: JSON.stringify(intent.body),
      })
      return responseJson<unknown>(response)
    },
    retry: (count, caught) => count < 2 && (
      !(caught instanceof ClientResponseError) || caught.status >= 500
    ),
    onMutate: (intent) => {
      const previous = optimisticView
      setOptimisticView({ base: candidate, value: intent.optimistic(view) })
      setError("")
      return { previous }
    },
    onError: (caught, _intent, context) => {
      setOptimisticView(context?.previous ?? null)
      if (reauthenticateExpiredSession(caught, router)) return
      setError("The change was not saved. The authoritative candidate state has been restored.")
    },
    onSuccess: (_result, intent) => {
      intentKeys.current.delete(intent.fingerprint)
      intent.afterSuccess?.()
      setMessage(intent.successMessage)
      onRevalidate?.()
    },
    onSettled: () => {
      intentPending.current = false
    },
  })

  function apply(
    path: string,
    method: Intent["method"],
    body: Record<string, unknown>,
    optimistic: Intent["optimistic"],
    successMessage: string,
    afterSuccess?: () => void,
  ) {
    if (mutation.isPending || intentPending.current) return
    const fingerprint = `${method}:${path}:${JSON.stringify(body)}`
    const key = intentKeys.current.get(fingerprint) ?? crypto.randomUUID()
    intentKeys.current.set(fingerprint, key)
    intentPending.current = true
    mutation.mutate({
      fingerprint,
      key,
      path,
      method,
      body,
      optimistic,
      successMessage,
      afterSuccess,
    })
  }

  function transition(stage: "Reviewed" | "Shortlisted") {
    apply(
      `/api/bff/job-candidates/${view.id}/stage`,
      "PATCH",
      { stage },
      (current) => ({ ...current, stage }),
      `Candidate moved to ${stage}.`,
    )
  }

  const evidence = matchEvidence(view.score_json)
  const groups = {
    supported: evidence.criteria.filter((item) => item.state === "supported"),
    failed: evidence.criteria.filter((item) => item.state === "failed"),
    unknown: evidence.criteria.filter((item) => item.state === "unknown"),
  }
  const components = [
    ["Role & skills", evidence.breakdown.role_and_skills, 35],
    ["Scope & seniority", evidence.breakdown.scope_seniority_years, 25],
    ["Industry", evidence.breakdown.industry, 20],
    ["Location & eligibility", evidence.breakdown.location_and_eligibility, 10],
    ["Recency & trajectory", evidence.breakdown.recency_and_trajectory, 10],
  ] as const

  return (
    <article className="candidate-detail" ref={article} tabIndex={-1} aria-labelledby="candidate-detail-heading">
      <header className="candidate-detail-header">
        <div>
          <p className="eyebrow">{view.classification === "near_match" ? "Near match" : "Ranked candidate"}</p>
          <h2 id="candidate-detail-heading">{view.full_name}</h2>
          <p>{[view.current_title, view.current_company, view.location].filter(Boolean).join(" · ")}</p>
        </div>
        <div className="detail-score"><strong>{view.score}</strong><span>/100</span></div>
      </header>
      <div className="version-line">
        <span>{view.scorecard_version ? `Scorecard version ${view.scorecard_version}` : `Scorecard ${view.scorecard_version_id}`}</span>
        <span>Scoring {view.scoring_version}</span>
        <span className="stage-pill">{view.stage}</span>
      </div>

      {error ? <p ref={errorBox} tabIndex={-1} className="form-error" role="alert">{error}</p> : null}
      <p className="sr-only" role="status" aria-live="polite">{message}</p>

      <section className="detail-section evidence-grid" aria-label="Match evidence">
        {(["failed", "unknown", "supported"] as const).map((state) => (
          <div key={state}>
            <h3>{state === "supported" ? "Supported facts" : state === "failed" ? "Failed facts" : "Unknowns"}</h3>
            {groups[state].length ? (
              <ul>{groups[state].map((item) => <li key={item.key}>{item.summary}</li>)}</ul>
            ) : <p>None recorded.</p>}
          </div>
        ))}
      </section>

      <section className="detail-section" aria-labelledby="score-components-heading">
        <h3 id="score-components-heading">Score components</h3>
        <dl className="score-components">
          {components.map(([label, value, maximum]) => (
            <div key={label}><dt>{label}</dt><dd>{value ?? 0} / {maximum}</dd></div>
          ))}
        </dl>
      </section>

      <section className="detail-section" aria-labelledby="experience-heading">
        <h3 id="experience-heading">Normalized experience</h3>
        {view.experiences?.length ? (
          <ol className="experience-list">
            {view.experiences.map((experience, index) => (
              <li key={`${experience.provider}-${index}`}>
                <strong>{experience.title ?? "Title unavailable"}</strong>
                <span>{experience.company_name ?? "Company unavailable"}</span>
                <small>{experience.start_date ?? "Start unknown"} — {experience.end_date ?? "Present or end unknown"}</small>
              </li>
            ))}
          </ol>
        ) : <p>No normalized experience was stored.</p>}
      </section>

      <section className="detail-section" aria-labelledby="provenance-heading">
        <h3 id="provenance-heading">Provider provenance</h3>
        {view.provenance?.length ? (
          <ul className="provenance-list">{view.provenance.map((item) => (
            <li key={`${item.field_name}-${item.provider}`}>{item.field_name.replaceAll("_", " ")} · {item.provider}</li>
          ))}</ul>
        ) : <p>No field provenance is available.</p>}
      </section>

      <ContactReveal
        candidateId={view.candidate_id}
        contacts={view.contacts ?? []}
        runCandidateId={view.run_candidate_id}
        enrichmentEligible={view.enrichment_eligible}
        estimatedEnrichmentCredits={view.estimated_enrichment_credits}
      />

      <section className="detail-section action-section" aria-labelledby="actions-heading">
        <h3 id="actions-heading">Review actions</h3>
        <div className="action-row">
          {view.stage !== "Reviewed" ? <button type="button" className="button button-secondary" disabled={mutation.isPending} onClick={() => transition("Reviewed")}>Mark Reviewed</button> : null}
          {view.stage !== "Shortlisted" && view.stage !== "Rejected" ? <button type="button" className="button button-primary" disabled={mutation.isPending} onClick={() => transition("Shortlisted")}>Shortlist</button> : null}
          {view.stage !== "Rejected" ? <button type="button" className="button button-danger-quiet" disabled={mutation.isPending} onClick={() => setRejecting(true)}>Reject</button> : null}
        </div>
        <div className="inline-forms">
          <form onSubmit={(event) => {
            event.preventDefault()
            const body = note.trim()
            if (!body) return
            apply(`/api/bff/job-candidates/${view.id}/notes`, "POST", { body }, (current) => current, "Note added.", () => setNote(""))
          }}>
            <label htmlFor={`candidate-note-${view.id}`}>Add note</label>
            <textarea id={`candidate-note-${view.id}`} value={note} maxLength={5_000} onChange={(event) => setNote(event.target.value)} />
            <button type="submit" className="button button-quiet" disabled={mutation.isPending || !note.trim()}>Add Note</button>
          </form>
          <form onSubmit={(event) => {
            event.preventDefault()
            const nextTags = tags.split(",").map((tag) => tag.trim()).filter(Boolean).slice(0, 20)
            apply(`/api/bff/job-candidates/${view.id}/tags`, "PUT", { tags: nextTags }, (current) => ({ ...current, tags: nextTags }), "Tags updated.")
          }}>
            <label htmlFor={`candidate-tags-${view.id}`}>Tags</label>
            <input id={`candidate-tags-${view.id}`} value={tags} onChange={(event) => setTagDraft({
              candidateId: candidate.id,
              authoritative: authoritativeTags,
              value: event.target.value,
            })} />
            <button type="submit" className="button button-quiet" disabled={mutation.isPending}>Save tags</button>
          </form>
          <form onSubmit={(event) => {
            event.preventDefault()
            const value = new FormData(event.currentTarget).get("owner")
            const owner = typeof value === "string" && value ? value : null
            apply(`/api/bff/job-candidates/${view.id}/owner`, "PATCH", { owner_user_id: owner }, (current) => ({ ...current, owner_user_id: owner }), "Owner updated.")
          }}>
            <label htmlFor={`candidate-owner-${view.id}`}>Owner</label>
            <select id={`candidate-owner-${view.id}`} name="owner" defaultValue={view.owner_user_id ?? ""}>
              <option value="">Unassigned</option>
              {ownerOptions.map((owner) => <option key={owner.userId} value={owner.userId}>{owner.name}</option>)}
            </select>
            <button type="submit" className="button button-quiet" disabled={mutation.isPending}>Assign Owner</button>
          </form>
        </div>
      </section>

      <section className="detail-section" aria-labelledby="notes-heading">
        <h3 id="notes-heading">Notes</h3>
        {view.notes?.length ? <ul>{view.notes.map((item) => <li key={item.id}>{item.body}</li>)}</ul> : <p>No notes yet.</p>}
      </section>

      {rejecting ? (
        <ModalDialog labelledBy="reject-heading" initialFocus={rejectionReason} onClose={() => setRejecting(false)}>
            <h3 id="reject-heading">Reject candidate</h3>
            <label htmlFor="rejection-reason">Reason</label>
            <select ref={rejectionReason} id="rejection-reason" value={reason} onChange={(event) => setReason(event.target.value)}>
              <option value="">Choose a controlled reason</option>
              {rejectionReasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <label htmlFor="rejection-note">Optional note</label>
            <textarea id="rejection-note" maxLength={2_000} value={rejectNote} onChange={(event) => setRejectNote(event.target.value)} />
            <div className="dialog-actions">
              <button type="button" className="button button-secondary" onClick={() => setRejecting(false)}>Cancel</button>
              <button type="button" className="button button-primary" disabled={!reason || mutation.isPending} onClick={() => {
                const reasonCode = reason
                setRejecting(false)
                apply(
                  `/api/bff/job-candidates/${view.id}/stage`,
                  "PATCH",
                  { stage: "Rejected", reason_code: reasonCode, note: rejectNote.trim() || null },
                  (current) => ({ ...current, stage: "Rejected", rejection_reason_code: reasonCode, rejection_note: rejectNote.trim() || null }),
                  "Candidate rejected.",
                )
              }}>Confirm rejection</button>
            </div>
        </ModalDialog>
      ) : null}
    </article>
  )
}
