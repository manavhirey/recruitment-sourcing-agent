import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { NearMatches } from "@/components/candidates/NearMatches"
import { nearMatchFixture } from "@/tests/review-fixtures"

describe("NearMatches", () => {
  it("renders failed and unknown mandatory criteria before the score", () => {
    render(<NearMatches candidates={[{
      ...nearMatchFixture,
      score_json: undefined,
      mandatory_gaps: [
        { key: "payments", label: "Payments experience", state: "failed", summary: "Stored evidence does not support Payments experience." },
        { key: "work_eligibility", label: "Work eligibility", state: "unknown", summary: "Evidence for Work eligibility is unknown." },
      ],
    }]} />)
    const failed = screen.getByText("Stored evidence does not support Payments experience.")
    const unknown = screen.getByText("Evidence for Work eligibility is unknown.")
    const score = screen.getByText("68 / 100")
    expect(failed.compareDocumentPosition(score) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(unknown.compareDocumentPosition(score) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText(/Near Match stage/i)).not.toBeInTheDocument()
  })
})
