import type { JobCandidate } from "@/lib/schemas"

export function NearMatches({ candidates }: { candidates: readonly JobCandidate[] }) {
  if (candidates.length === 0) {
    return <div className="empty-state compact"><h2>No near matches</h2><p>Candidates with failed or mandatory unknown criteria will appear here.</p></div>
  }
  return (
    <ul className="near-match-list" aria-label="Near matches">
      {candidates.map((candidate) => {
        const mandatory = candidate.mandatory_gaps ?? []
        return (
          <li key={candidate.id}>
            <div><p className="eyebrow">Near match</p><h3>{candidate.full_name}</h3></div>
            <ul className="mandatory-gaps">
              {mandatory.map((criterion) => (
                <li key={criterion.key} className={`evidence-${criterion.state}`}>{criterion.summary}</li>
              ))}
            </ul>
            <p className="near-score"><strong>{candidate.score} / 100</strong></p>
          </li>
        )
      })}
    </ul>
  )
}
