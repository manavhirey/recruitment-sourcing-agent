"use client"

import type { JobCandidate } from "@/lib/schemas"

export function RankedCandidateList({
  candidates,
  selectedId,
  onSelect,
}: {
  candidates: readonly JobCandidate[]
  selectedId: string | null
  onSelect: (candidate: JobCandidate) => void
}) {
  if (candidates.length === 0) {
    return <div className="candidate-list-empty"><h3>No ranked candidates yet</h3><p>Results appear as matching completes.</p></div>
  }
  return (
    <ol className="ranked-list" aria-label="Ranked candidates">
      {candidates.map((candidate, index) => (
        <li key={candidate.id}>
          <button
            type="button"
            aria-pressed={selectedId === candidate.id}
            onClick={() => onSelect(candidate)}
          >
            <span className="rank-number">{index + 1}</span>
            <span className="rank-person">
              <strong>{candidate.full_name}</strong>
              <small>{[candidate.current_title, candidate.current_company].filter(Boolean).join(" · ") || "Profile details unavailable"}</small>
            </span>
            <span className="rank-score">{candidate.score}<small>/100</small></span>
          </button>
        </li>
      ))}
    </ol>
  )
}
