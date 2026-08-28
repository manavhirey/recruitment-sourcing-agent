import type { ScorecardDraftResponse, SeniorityOption } from "@/lib/schemas"

export const seniorityOptionsFixture = [
  { value: "early_career", label: "Early-Career", minimum_years: 0, maximum_years: 3 },
  { value: "mid_level", label: "Mid-Level", minimum_years: 3, maximum_years: 9 },
  { value: "senior", label: "Senior", minimum_years: 10, maximum_years: null },
] as const satisfies readonly SeniorityOption[]

export const scorecardDraftFixture = {
  job_id: "00000000-0000-4000-8000-000000000101",
  draft_revision: 2,
  original_job_description: "Hire a senior product manager for our payments platform.",
  extraction_status: "ready" as const,
  extraction_warning: null,
  seniority_options: seniorityOptionsFixture,
  draft: {
    target_titles: ["Senior Product Manager"],
    criteria: [
      {
        key: "payments",
        label: "Payments platform experience",
        kind: "must_have" as const,
        evidence_required: true,
        source_text: "payments platform experience",
        inferred: false,
        recruiter_entered: false,
        lawful_requirement_confirmed: false,
      },
      {
        key: "growth",
        label: "Led product-led growth",
        kind: "preference" as const,
        evidence_required: false,
        source_text: null,
        inferred: true,
        recruiter_entered: false,
        lawful_requirement_confirmed: false,
      },
    ],
    seniority: ["senior"],
    minimum_years: 5,
    maximum_years: null,
    locations: ["New York, NY"],
    industry_code: "technology.fintech",
    suggested_adjacent_industries: ["financial_services.banking"],
    uncertainties: ["Confirm ownership of go-to-market strategy"],
    confirmed_inferred_items: [],
  },
} satisfies ScorecardDraftResponse

export const manualRequiredDraftFixture = {
  ...scorecardDraftFixture,
  extraction_status: "manual_required" as const,
  extraction_warning: "Automated extraction could not be completed.",
  draft_revision: 1,
  draft: {
    target_titles: [],
    criteria: [],
    seniority: [],
    minimum_years: null,
    maximum_years: null,
    locations: [],
    industry_code: "",
    suggested_adjacent_industries: [],
    uncertainties: [],
    confirmed_inferred_items: [],
  },
} satisfies ScorecardDraftResponse

export const authorizedClientsFixture = [
  {
    id: "00000000-0000-4000-8000-000000000201",
    tenant_id: "00000000-0000-4000-8000-000000000001",
    name: "PayFlow",
    industry_codes: ["technology.fintech"],
    adjacent_industries: [["technology.fintech", "financial_services.banking"]],
  },
] as const
