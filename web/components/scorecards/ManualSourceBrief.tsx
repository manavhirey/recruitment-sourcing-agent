import "server-only"

export function ManualSourceBrief({ jobDescription }: { jobDescription: string }) {
  return (
    <details className="source-brief">
      <summary>Review original job description</summary>
      <p className="source-brief-copy">{jobDescription}</p>
    </details>
  )
}
