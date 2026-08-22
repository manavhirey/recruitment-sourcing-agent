"use client"

import { useQuery } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"

import { responseJson } from "@/lib/client-response"
import type {
  CandidateDirectoryPage,
  CandidateJob,
} from "@/lib/schemas"

export function CandidateDirectory({
  initialPage,
}: {
  initialPage: CandidateDirectoryPage
}) {
  const [draft, setDraft] = useState("")
  const [queryText, setQueryText] = useState("")
  const [cursor, setCursor] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const errorFocus = useRef<HTMLParagraphElement>(null)
  const page = useQuery({
    queryKey: ["candidate-directory", queryText, cursor],
    initialData: !queryText && !cursor ? initialPage : undefined,
    queryFn: async () => {
      const search = new URLSearchParams({ limit: "50" })
      if (queryText) search.set("q", queryText)
      if (cursor) search.set("cursor", cursor)
      return responseJson<CandidateDirectoryPage>(await fetch(`/api/bff/candidates?${search}`))
    },
  })
  const jobs = useQuery({
    queryKey: ["candidate-directory-jobs", selectedId],
    enabled: Boolean(selectedId),
    queryFn: async () => responseJson<CandidateJob[]>(await fetch(`/api/bff/candidates/${selectedId}/jobs`)),
  })

  useEffect(() => {
    if (page.isError || jobs.isError) errorFocus.current?.focus()
  }, [jobs.isError, page.isError])

  return (
    <div className="candidate-directory">
      <form className="directory-search" onSubmit={(event) => {
        event.preventDefault()
        const normalized = draft.trim()
        setQueryText(normalized)
        setCursor(null)
        setSelectedId(null)
        window.history.replaceState({}, "", window.location.pathname)
      }}>
        <div className="field"><label htmlFor="candidate-search">Search candidates</label><input id="candidate-search" value={draft} maxLength={255} onChange={(event) => setDraft(event.target.value)} /></div>
        <button className="button button-primary" type="submit">Search</button>
      </form>
      {(page.isError || jobs.isError) ? <p ref={errorFocus} tabIndex={-1} className="form-error" role="alert">Candidate directory is temporarily unavailable.</p> : null}
      {page.isPending ? <p role="status">Searching authorized candidates…</p> : null}
      <div className="directory-results">
        <section aria-labelledby="directory-results-heading">
          <h2 id="directory-results-heading">Authorized candidates</h2>
          {page.data?.items.length ? (
            <ul className="directory-list">{page.data.items.map((candidate) => (
              <li key={candidate.id}><button type="button" aria-pressed={selectedId === candidate.id} onClick={() => setSelectedId(candidate.id)}><strong>{candidate.name}</strong><span>{[candidate.current_title, candidate.current_company, candidate.location].filter(Boolean).join(" · ")}</span></button></li>
            ))}</ul>
          ) : page.data ? <div className="empty-state compact"><h2>No authorized matches</h2><p>Try a different name, title, company, skill, or experience term.</p></div> : null}
          {page.data?.next_cursor ? <button className="button button-secondary" type="button" onClick={() => setCursor(page.data?.next_cursor ?? null)}>Next page</button> : null}
        </section>
        <section aria-labelledby="job-history-heading">
          <h2 id="job-history-heading">Authorized job history</h2>
          {selectedId && jobs.isPending ? <p role="status">Loading job history…</p> : null}
          {jobs.data?.length ? <ul className="job-history">{jobs.data.map((job) => <li key={job.job_candidate_id}><a href={`/jobs/${job.job_id}?candidate=${job.job_candidate_id}`}><strong>{job.job_title}</strong><span>{job.classification === "near_match" ? "Near Match" : "Ranked"} · {job.score} · {job.stage}</span></a></li>)}</ul> : selectedId && !jobs.isPending && !jobs.isError ? <p>No authorized job history is available.</p> : !selectedId ? <p>Select a candidate to open job history.</p> : null}
        </section>
      </div>
    </div>
  )
}
