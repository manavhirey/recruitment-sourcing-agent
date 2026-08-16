import { describe, expect, it } from "vitest"

import { requiredInferenceIds } from "@/lib/inference-confirmations"
import { scorecardDraftFixture } from "@/tests/fixtures"

describe("inference confirmation identifiers", () => {
  it("binds every required approval to its category and exact content", () => {
    const original = requiredInferenceIds(scorecardDraftFixture.draft)
    const edited = requiredInferenceIds({
      ...scorecardDraftFixture.draft,
      criteria: scorecardDraftFixture.draft.criteria.map((criterion) =>
        criterion.key === "growth"
          ? { ...criterion, label: "Owned product-led growth" }
          : criterion,
      ),
    })

    expect(original).toHaveLength(3)
    expect(original.map((item) => item.split(":", 1)[0]).sort()).toEqual([
      "adjacent",
      "criterion",
      "uncertainty",
    ])
    expect(edited).not.toEqual(original)
  })
})
