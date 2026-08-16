"use client"

import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"

import { ActivityPanel } from "@/components/candidates/ActivityPanel"
import { CandidateDetail } from "@/components/candidates/CandidateDetail"
import { CandidateTable, type CandidateTableFilters } from "@/components/candidates/CandidateTable"
import { NearMatches } from "@/components/candidates/NearMatches"
import { RankedCandidateList } from "@/components/candidates/RankedCandidateList"
import { responseJson } from "@/lib/client-response"
import type {
  ConfirmedScorecard,
  JobCandidate,
  JobCandidatePage,
} from "@/lib/schemas"

export type WorkspaceTab = "review" | "all" | "near" | "scorecard" | "activity"
type OwnerOption = { userId: string; name: string }
const tabs: readonly { id: WorkspaceTab; label: string }[] = [
  { id: "review", label: "Review" },
  { id: "all", label: "All Candidates" },
  { id: "near", label: "Near Matches" },
  { id: "scorecard", label: "Scorecard" },
  { id: "activity", label: "Run Activity" },
]

function safeTab(value: string | null): WorkspaceTab {
  return tabs.some((tab) => tab.id === value) ? value as WorkspaceTab : "review"
}

export function ReviewWorkspace({
  jobId,
  runId,
  initialCandidates,
  initialSelectedCandidate,
  initialNearMatches,
  immutableScorecard,
  ownerOptions = [],
  initialTab = "review",
  initialTablePage,
  initialTableFilters,
}: {
  jobId: string
  runId?: string | null
  initialCandidates: JobCandidatePage
  initialSelectedCandidate?: JobCandidate | null
  initialNearMatches: JobCandidatePage
  immutableScorecard: ConfirmedScorecard | null
  ownerOptions?: readonly OwnerOption[]
  initialTab?: WorkspaceTab
  initialTablePage?: JobCandidatePage
  initialTableFilters?: CandidateTableFilters
}) {
  const queryClient = useQueryClient()
  const errorFocus = useRef<HTMLParagraphElement>(null)
  const detailPanel = useRef<HTMLDivElement>(null)
  const requestedCandidateFocus = useRef<string | null>(null)
  const firstId = initialSelectedCandidate?.id ?? initialCandidates.items[0]?.id ?? null
  const [selectedId, setSelectedId] = useState(firstId)
  const [activeTab, setActiveTab] = useState<WorkspaceTab>(safeTab(initialTab))
  const candidates = useQuery({
    queryKey: ["job-candidates", jobId, "review"],
    initialData: initialCandidates,
    queryFn: async () => responseJson<JobCandidatePage>(
      await fetch(`/api/bff/jobs/${jobId}/candidates?classification=main&sort=-score&limit=50`),
    ),
  })
  const nearMatches = useQuery({
    queryKey: ["job-candidates", jobId, "near"],
    initialData: initialNearMatches,
    queryFn: async () => responseJson<JobCandidatePage>(
      await fetch(`/api/bff/jobs/${jobId}/candidates?classification=near_match&sort=-score&limit=50`),
    ),
  })
  const selectedFromPage = candidates.data.items.find((item) => item.id === selectedId)
  const selected = useQuery({
    queryKey: ["job-candidate", selectedId],
    enabled: Boolean(selectedId),
    initialData: selectedId === initialSelectedCandidate?.id
      ? initialSelectedCandidate
      : selectedFromPage?.score_json
        ? selectedFromPage
        : undefined,
    queryFn: async () => responseJson<JobCandidate>(await fetch(`/api/bff/job-candidates/${selectedId}`)),
  })

  useEffect(() => {
    if (selected.isError) errorFocus.current?.focus()
  }, [selected.isError])

  useEffect(() => {
    if (selected.data?.id === requestedCandidateFocus.current) {
      detailPanel.current?.querySelector<HTMLElement>(".candidate-detail")?.focus()
      requestedCandidateFocus.current = null
    }
  }, [selected.data?.id])

  function updateUrl(nextTab: WorkspaceTab, candidateId: string | null) {
    const search = new URLSearchParams()
    if (nextTab !== "review") search.set("tab", nextTab)
    if (candidateId) search.set("candidate", candidateId)
    const suffix = search.size ? `?${search}` : ""
    window.history.replaceState({}, "", `${window.location.pathname}${suffix}`)
  }

  function choose(candidate: JobCandidate) {
    requestedCandidateFocus.current = candidate.id
    setSelectedId(candidate.id)
    setActiveTab("review")
    updateUrl("review", candidate.id)
  }

  function activateTab(tab: WorkspaceTab) {
    setActiveTab(tab)
    updateUrl(tab, selectedId)
  }

  return (
    <div className="review-workspace">
      <div className="workspace-tabs" role="tablist" aria-label="Job workspace">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            id={`tab-${tab.id}`}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={`panel-${tab.id}`}
            tabIndex={activeTab === tab.id ? 0 : -1}
            onClick={() => activateTab(tab.id)}
            onKeyDown={(event) => {
              const current = tabs.findIndex((item) => item.id === tab.id)
              const destination = event.key === "ArrowRight"
                ? (current + 1) % tabs.length
                : event.key === "ArrowLeft"
                  ? (current - 1 + tabs.length) % tabs.length
                  : event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? tabs.length - 1
                      : null
              if (destination === null) return
              event.preventDefault()
              const next = tabs[destination]
              activateTab(next.id)
              document.getElementById(`tab-${next.id}`)?.focus()
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "review" ? (
        <section id="panel-review" role="tabpanel" aria-labelledby="tab-review" className="review-split">
          <aside className="ranked-panel" aria-label="Ranked candidate list">
            <RankedCandidateList candidates={candidates.data.items} selectedId={selectedId} onSelect={choose} />
            {candidates.data.next_cursor ? <p className="field-hint">Open All Candidates for server-paginated results.</p> : null}
          </aside>
          <div className="detail-panel" ref={detailPanel}>
            {selectedId && selected.isPending ? <div className="detail-loading" role="status">Loading candidate evidence…</div> : null}
            {selectedId && selected.isError ? <p ref={errorFocus} tabIndex={-1} className="form-error" role="alert">Candidate detail is unavailable. Choose another candidate or retry.</p> : null}
            {selected.data ? (
              <CandidateDetail
                key={`${selected.data.id}:${selected.data.updated_at}`}
                candidate={selected.data}
                ownerOptions={ownerOptions}
                onRevalidate={() => {
                  void queryClient.invalidateQueries({ queryKey: ["job-candidate", selectedId] })
                  void queryClient.invalidateQueries({ queryKey: ["job-candidates", jobId] })
                }}
              />
            ) : !selectedId || (!selected.isPending && !selected.isError) ? (
              <div className="empty-state compact"><h2>Select a candidate</h2><p>Evidence-backed detail will open here.</p></div>
            ) : null}
          </div>
        </section>
      ) : null}
      {activeTab === "all" ? <section id="panel-all" role="tabpanel" aria-labelledby="tab-all"><CandidateTable jobId={jobId} initialPage={initialTablePage ?? (initialTableFilters ? undefined : initialCandidates)} initialFilters={initialTableFilters} ownerOptions={ownerOptions} /></section> : null}
      {activeTab === "near" ? <section id="panel-near" role="tabpanel" aria-labelledby="tab-near"><NearMatches candidates={nearMatches.data.items} /></section> : null}
      {activeTab === "scorecard" ? (
        <section id="panel-scorecard" role="tabpanel" aria-labelledby="tab-scorecard" className="scorecard-readonly">
          {immutableScorecard ? (
            <>
              <header><p className="eyebrow">Immutable run scorecard</p><h2>Version {immutableScorecard.version}</h2></header>
              <dl><div><dt>Target titles</dt><dd>{immutableScorecard.target_titles.join(", ")}</dd></div><div><dt>Industry</dt><dd>{immutableScorecard.industry_code}</dd></div><div><dt>Scored from</dt><dd>{immutableScorecard.confirmed_at}</dd></div></dl>
              <ul>{immutableScorecard.criteria.map((criterion) => <li key={criterion.key}><strong>{criterion.label}</strong><span>{criterion.kind.replaceAll("_", " ")}{criterion.evidence_required ? " · evidence mandatory" : ""}</span></li>)}</ul>
            </>
          ) : <div className="empty-state compact"><h2>Scorecard unavailable</h2><p>This run did not return an authorized immutable scorecard.</p></div>}
        </section>
      ) : null}
      {activeTab === "activity" ? <section id="panel-activity" role="tabpanel" aria-labelledby="tab-activity"><ActivityPanel runId={runId} jobCandidateId={selectedId} /></section> : null}
    </div>
  )
}
