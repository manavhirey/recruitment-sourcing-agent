import { describe, expect, it } from "vitest"

import type { components } from "@/lib/generated-api"
import { scorecardDraftRequest } from "@/lib/request-schemas"
import { scorecardDraftFixture } from "@/tests/fixtures"

type EditableScorecardDraft = components["schemas"]["EditableScorecardDraft"]

function requestDraft(overrides: Partial<EditableScorecardDraft> = {}) {
  return {
    ...scorecardDraftFixture.draft,
    confirmed_inferred_items: [],
    ...overrides,
  }
}

describe("scorecard draft request schema", () => {
  it.each([
    { seniority: [], minimum_years: null, maximum_years: null },
    { seniority: ["early_career"], minimum_years: 2, maximum_years: null },
    { seniority: ["mid_level", "senior"], minimum_years: null, maximum_years: 12 },
    { seniority: ["early_career", "mid_level", "senior"], minimum_years: 5, maximum_years: 5 },
  ])("accepts canonical presets and supported inclusive bound shapes: %#", (range) => {
    expect(scorecardDraftRequest.safeParse(requestDraft(range)).success).toBe(true)
  })

  it("rejects unknown seniority values and inverted custom bounds", () => {
    expect(scorecardDraftRequest.safeParse(requestDraft({ seniority: ["manager"] })).success)
      .toBe(false)
    expect(scorecardDraftRequest.safeParse(requestDraft({
      seniority: ["senior"],
      minimum_years: 10,
      maximum_years: 9,
    })).success).toBe(false)
  })
})
