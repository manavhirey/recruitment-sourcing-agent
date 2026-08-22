import type { JobCandidate } from "@/lib/schemas"

const base = {
  job_id: "00000000-0000-4000-8000-000000000101",
  candidate_id: "00000000-0000-4000-8000-000000000601",
  run_candidate_id: "00000000-0000-4000-8000-000000000611",
  current_title: "Senior Product Manager",
  current_company: "PayFlow",
  location: "New York, United States",
  classification: "main",
  scorecard_version_id: "00000000-0000-4000-8000-000000000403",
  scorecard_version: 3,
  scoring_version: "matching-v1",
  stage: "New" as const,
  owner_user_id: null,
  rejection_reason_code: null,
  rejection_note: null,
  tags: ["Payments"],
  has_contact: true,
  enrichment_eligible: false,
  estimated_enrichment_credits: null,
  mandatory_gaps: [],
  contacts: [],
  experiences: [{
    title: "Senior Product Manager",
    company_name: "PayFlow",
    start_date: "2021-01",
    end_date: null,
    provider: "apollo",
    source_timestamp: "2026-08-15T00:00:00Z",
  }],
  provenance: [{
    field_name: "current_title",
    provider: "apollo",
    source_timestamp: "2026-08-15T00:00:00Z",
  }],
  notes: [],
  created_at: "2026-08-15T00:00:00Z",
  updated_at: "2026-08-16T00:00:00Z",
}

export const priyaCandidateFixture = {
  ...base,
  id: "00000000-0000-4000-8000-000000000501",
  full_name: "Priya Sharma",
  score: 92,
  score_json: {
    total: 92,
    breakdown: {
      role_and_skills: 34,
      scope_seniority_years: 23,
      industry: 18,
      location_and_eligibility: 8,
      recency_and_trajectory: 9,
    },
    criteria: [
      { key: "payments", label: "Payments experience", state: "supported", summary: "5 years in payments and fintech", points: 20, max_points: 20, evidence: ["5 years in payments and fintech"], source_refs: ["apollo:experience:0"] },
      { key: "us_market", label: "US market experience", state: "unknown", summary: "US market experience is unknown", points: 0, max_points: 5, evidence: [], source_refs: [] },
    ],
    failed_must_haves: [],
    unknown_keys: ["us_market"],
  },
} satisfies JobCandidate

export const marcusCandidateFixture = {
  ...base,
  id: "00000000-0000-4000-8000-000000000502",
  candidate_id: "00000000-0000-4000-8000-000000000602",
  run_candidate_id: "00000000-0000-4000-8000-000000000612",
  full_name: "Marcus Lee",
  score: 84,
  score_json: { total: 84, breakdown: {}, criteria: [], failed_must_haves: [], unknown_keys: [] },
} satisfies JobCandidate

export const nearMatchFixture = {
  ...base,
  id: "00000000-0000-4000-8000-000000000503",
  candidate_id: "00000000-0000-4000-8000-000000000603",
  run_candidate_id: "00000000-0000-4000-8000-000000000613",
  full_name: "Avery Stone",
  classification: "near_match",
  score: 68,
  score_json: {
    total: 68,
    breakdown: {},
    criteria: [
      { key: "payments", label: "Payments experience", state: "failed", summary: "Missing required payments experience", points: 0, max_points: 20, evidence: [], source_refs: [] },
      { key: "work_eligibility", label: "Work eligibility", state: "unknown", summary: "Work eligibility is unknown", points: 0, max_points: 10, evidence: [], source_refs: [] },
    ],
    failed_must_haves: ["payments"],
    unknown_keys: ["work_eligibility"],
  },
} satisfies JobCandidate
