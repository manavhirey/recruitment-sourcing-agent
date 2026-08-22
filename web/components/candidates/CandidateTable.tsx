"use client"

import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useRouter } from "next/navigation"
import { useEffect, useRef, useState } from "react"

import {
  ClientResponseError,
  reauthenticateExpiredSession,
  requireResponse,
  responseJson,
} from "@/lib/client-response"
import type { JobCandidatePage } from "@/lib/schemas"

const bulkConcurrency = 4
const stages = ["New", "Reviewed", "Shortlisted", "Rejected"] as const

function safeStage(value: string | null): string {
  return stages.includes(value as typeof stages[number]) ? value ?? "" : ""
}

function safeContact(value: string | null): "" | "true" | "false" {
  return value === "true" || value === "false" ? value : ""
}

export type CandidateTableFilters = {
  stage: string
  hasContact: "" | "true" | "false"
  sort: "-score" | "score"
  cursor: string | null
}

const defaultFilters: CandidateTableFilters = {
  stage: "",
  hasContact: "",
  sort: "-score",
  cursor: null,
}

export function CandidateTable({
  jobId,
  initialPage,
  ownerOptions = [],
  initialFilters = defaultFilters,
}: {
  jobId: string
  initialPage?: JobCandidatePage
  ownerOptions?: readonly { userId: string; name: string }[]
  initialFilters?: CandidateTableFilters
}) {
  const router = useRouter()
  const queryClient = useQueryClient()
  const initialStage = safeStage(initialFilters.stage)
  const initialContact = safeContact(initialFilters.hasContact)
  const initialSort = initialFilters.sort
  const initialCursor = initialFilters.cursor
  const [stage, setStage] = useState(initialStage)
  const [hasContact, setHasContact] = useState(initialContact)
  const [sort, setSort] = useState(initialSort)
  const [cursor, setCursor] = useState<string | null>(initialCursor)
  const selectionScope = `${jobId}:${stage}:${hasContact}:${sort}:${cursor ?? ""}`
  const [selection, setSelection] = useState<{
    scope: string
    ids: string[]
  }>({ scope: selectionScope, ids: [] })
  const selected = selection.scope === selectionScope ? selection.ids : []
  const [bulkAction, setBulkAction] = useState<"stage" | "owner">("stage")
  const [bulkStage, setBulkStage] = useState<"Reviewed" | "Shortlisted">("Reviewed")
  const [bulkOwner, setBulkOwner] = useState(ownerOptions[0]?.userId ?? "")
  const [result, setResult] = useState<string>("")
  const [busy, setBusy] = useState(false)
  const [exportIntentKey, setExportIntentKey] = useState("")
  const intentKeys = useRef(new Map<string, string>())
  const exportKey = useRef<HTMLInputElement>(null)
  const exportSubmitting = useRef(false)
  const exportReleaseTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    const reset = () => { exportSubmitting.current = false }
    window.addEventListener("pageshow", reset)
    return () => {
      window.removeEventListener("pageshow", reset)
      if (exportReleaseTimer.current) clearTimeout(exportReleaseTimer.current)
    }
  }, [])
  const query = useQuery({
    queryKey: ["job-candidates", jobId, "table", stage, hasContact, sort, cursor],
    initialData: initialPage && stage === initialStage && hasContact === initialContact && sort === initialSort && cursor === initialCursor
      ? initialPage
      : undefined,
    queryFn: async () => {
      const search = new URLSearchParams({ classification: "main", sort, limit: "50" })
      if (stage) search.set("stage", stage)
      if (hasContact) search.set("has_contact", hasContact)
      if (cursor) search.set("cursor", cursor)
      return responseJson<JobCandidatePage>(await fetch(`/api/bff/jobs/${jobId}/candidates?${search}`))
    },
  })

  function bindUrl(nextStage: string, nextContact: string, nextSort: string, nextCursor: string | null = null) {
    const search = new URLSearchParams()
    search.set("tab", "all")
    if (nextStage) search.set("stage", nextStage)
    if (nextContact) search.set("has_contact", nextContact)
    if (nextSort !== "-score") search.set("sort", nextSort)
    if (nextCursor) search.set("cursor", nextCursor)
    window.history.replaceState({}, "", `${window.location.pathname}?${search}`)
  }

  async function boundedBulk() {
    const ids = [...selected]
    if (!ids.length || busy) return
    setBusy(true)
    setResult("")
    let succeeded = 0
    let failed = 0
    const queue = [...ids]
    const workers = Array.from({ length: Math.min(bulkConcurrency, queue.length) }, async () => {
      while (queue.length) {
        const id = queue.shift()
        if (!id) return
        const fingerprint = bulkAction === "stage"
          ? `bulk-stage:${id}:${bulkStage}`
          : `bulk-owner:${id}:${bulkOwner || "unassigned"}`
        const idempotencyKey = intentKeys.current.get(fingerprint) ?? crypto.randomUUID()
        intentKeys.current.set(fingerprint, idempotencyKey)
        try {
          const path = bulkAction === "stage"
            ? `/api/bff/job-candidates/${id}/stage`
            : `/api/bff/job-candidates/${id}/owner`
          const body = bulkAction === "stage"
            ? { stage: bulkStage }
            : { owner_user_id: bulkOwner || null }
          for (let attempt = 0; attempt < 2; attempt += 1) {
            try {
              await requireResponse(await fetch(path, {
                method: "PATCH",
                headers: {
                  "Content-Type": "application/json",
                  "Idempotency-Key": idempotencyKey,
                },
                body: JSON.stringify(body),
              }))
              break
            } catch (caught) {
              if (
                attempt === 1 ||
                caught instanceof ClientResponseError && caught.status < 500
              ) throw caught
            }
          }
          intentKeys.current.delete(fingerprint)
          succeeded += 1
        } catch (caught) {
          if (reauthenticateExpiredSession(caught, router)) return
          failed += 1
        }
      }
    })
    await Promise.all(workers)
    setResult(`Affected ${ids.length}. Succeeded ${succeeded}. Failed ${failed}.`)
    setSelection({ scope: selectionScope, ids: [] })
    setBusy(false)
    await queryClient.invalidateQueries({ queryKey: ["job-candidates", jobId] })
  }

  const page = query.data
  return (
    <section className="candidate-table-section" aria-labelledby="all-candidates-heading">
      <div className="table-controls">
        <div className="field"><label htmlFor="table-stage">Stage</label><select id="table-stage" value={stage} onChange={(event) => { setStage(event.target.value); setCursor(null); bindUrl(event.target.value, hasContact, sort) }}><option value="">All stages</option>{stages.map((value) => <option key={value}>{value}</option>)}</select></div>
        <div className="field"><label htmlFor="table-contact">Contact availability</label><select id="table-contact" value={hasContact} onChange={(event) => { const value = safeContact(event.target.value); setHasContact(value); setCursor(null); bindUrl(stage, value, sort) }}><option value="">Any</option><option value="true">Available (masked)</option><option value="false">Unavailable</option></select></div>
        <div className="field"><label htmlFor="table-sort">Sort</label><select id="table-sort" value={sort} onChange={(event) => { const value = event.target.value === "score" ? "score" : "-score"; setSort(value); setCursor(null); bindUrl(stage, hasContact, value) }}><option value="-score">Score high to low</option><option value="score">Score low to high</option></select></div>
        <div className="field"><label htmlFor="bulk-action">Bulk action</label><select id="bulk-action" value={bulkAction} onChange={(event) => setBulkAction(event.target.value as "stage" | "owner")}><option value="stage">Change stage</option><option value="owner">Assign owner</option></select></div>
        {bulkAction === "stage" ? <div className="field"><label htmlFor="bulk-stage">Bulk stage</label><select id="bulk-stage" value={bulkStage} onChange={(event) => setBulkStage(event.target.value as "Reviewed" | "Shortlisted")}><option>Reviewed</option><option>Shortlisted</option></select></div> : <div className="field"><label htmlFor="bulk-owner">Bulk owner</label><select id="bulk-owner" value={bulkOwner} onChange={(event) => setBulkOwner(event.target.value)}><option value="">Unassigned</option>{ownerOptions.map((owner) => <option key={owner.userId} value={owner.userId}>{owner.name}</option>)}</select></div>}
        <button type="button" className="button button-secondary" disabled={!selected.length || busy} onClick={() => void boundedBulk()}>Apply to {selected.length}</button>
        {stage === "Shortlisted" ? <form action={`/api/bff/jobs/${jobId}/export`} method="post" onSubmit={(event) => {
          if (exportSubmitting.current) {
            event.preventDefault()
            return
          }
          exportSubmitting.current = true
          if (!exportIntentKey) {
            const key = crypto.randomUUID()
            if (exportKey.current) exportKey.current.value = key
            setExportIntentKey(key)
          }
          exportReleaseTimer.current = setTimeout(() => {
            exportSubmitting.current = false
            exportReleaseTimer.current = null
          }, 1_000)
        }}><input ref={exportKey} type="hidden" name="idempotencyKey" value={exportIntentKey} readOnly /><button className="button button-primary" type="submit">{exportIntentKey ? "Retry shortlisted CSV export" : "Export shortlisted CSV"}</button></form> : null}
        {stage === "Shortlisted" && exportIntentKey ? <button type="button" className="button button-quiet" onClick={() => {
          if (exportSubmitting.current) return
          if (exportKey.current) exportKey.current.value = ""
          setExportIntentKey("")
        }}>Start new export</button> : null}
      </div>
      <p role="status" aria-live="polite">{result}</p>
      {query.isPending ? <p role="status">Loading candidate table…</p> : null}
      {query.isError ? <p role="alert" className="form-error">Candidates are temporarily unavailable.</p> : null}
      {page && page.items.length ? (
        <div className="table-scroll" tabIndex={0} aria-label="Scrollable candidate table">
          <table className="candidate-table">
            <thead><tr><th scope="col">Select</th><th scope="col">Candidate</th><th scope="col">Score</th><th scope="col">Stage</th><th scope="col">Location</th><th scope="col">Contact</th></tr></thead>
            <tbody>{page.items.map((candidate) => <tr key={candidate.id}><td><input type="checkbox" aria-label={`Select ${candidate.full_name}`} checked={selected.includes(candidate.id)} onChange={(event) => setSelection((current) => {
              const ids = current.scope === selectionScope ? current.ids : []
              return {
                scope: selectionScope,
                ids: event.target.checked ? [...ids, candidate.id] : ids.filter((id) => id !== candidate.id),
              }
            })} /></td><th scope="row">{candidate.full_name}</th><td>{candidate.score}</td><td>{candidate.stage}</td><td>{candidate.location ?? "Unknown"}</td><td>{candidate.has_contact ? "Available (masked)" : "Unavailable"}</td></tr>)}</tbody>
          </table>
        </div>
      ) : page ? <div className="empty-state compact"><h2>No candidates in this view</h2><p>Adjust the server-backed filters.</p></div> : null}
      {page?.next_cursor ? <button type="button" className="button button-secondary" onClick={() => {
        setSelection({ scope: selectionScope, ids: [] })
        setCursor(page.next_cursor)
        bindUrl(stage, hasContact, sort, page.next_cursor)
      }}>Next page</button> : null}
    </section>
  )
}
