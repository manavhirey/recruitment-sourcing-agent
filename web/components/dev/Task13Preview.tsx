"use client"

import { useState } from "react"

import { CandidateDirectory } from "@/components/candidates/CandidateDirectory"
import { ReviewWorkspace } from "@/components/candidates/ReviewWorkspace"
import { JobIntakeForm } from "@/components/jobs/JobIntakeForm"
import { RunStatus } from "@/components/jobs/RunStatus"
import { AgencyAlerts } from "@/components/layout/AgencyAlerts"
import { MembershipManager } from "@/components/layout/MembershipManager"
import { ScorecardEditor } from "@/components/scorecards/ScorecardEditor"
import type {
  Client,
  ConfirmedScorecard,
  JobCandidate,
  Member,
  Notification,
  Role,
  ScorecardDraftResponse,
  SourcingRun,
} from "@/lib/schemas"

const tenantId = "00000000-0000-4000-8000-000000000001"
const clientId = "00000000-0000-4000-8000-000000000201"
const jobId = "00000000-0000-4000-8000-000000000101"
const runId = "00000000-0000-4000-8000-000000000301"
const scorecardId = "00000000-0000-4000-8000-000000000403"

const client: Client = {
  id: clientId,
  tenant_id: tenantId,
  name: "PayFlow",
  industry_codes: ["technology.fintech"],
  adjacent_industries: [["technology.fintech", "financial_services.banking"]],
}

export const previewDraft: ScorecardDraftResponse = {
  job_id: jobId,
  draft_revision: 2,
  original_job_description: "Lead a payments platform and product-led growth strategy.",
  extraction_status: "ready",
  extraction_warning: null,
  draft: {
    target_titles: ["Senior Product Manager"],
    criteria: [
      {
        key: "payments",
        label: "Payments platform experience",
        kind: "must_have",
        evidence_required: true,
        source_text: "payments platform experience",
        inferred: false,
        recruiter_entered: false,
        lawful_requirement_confirmed: false,
      },
      {
        key: "growth",
        label: "Led product-led growth",
        kind: "preference",
        evidence_required: false,
        source_text: "product-led growth strategy",
        inferred: false,
        recruiter_entered: false,
        lawful_requirement_confirmed: false,
      },
    ],
    seniority: ["senior"],
    minimum_years: 5,
    maximum_years: null,
    locations: ["New York, NY"],
    industry_code: "technology.fintech",
    suggested_adjacent_industries: [],
    uncertainties: [],
    confirmed_inferred_items: [],
  },
}

export const previewRun: SourcingRun = {
  id: runId,
  tenant_id: tenantId,
  job_id: jobId,
  scorecard_version_id: scorecardId,
  state: "partially_ready",
  current_stage: "enrichment",
  candidate_count: 126,
  matched_count: 18,
  enriched_count: 12,
  failed_count: 1,
  budget_use: { estimated_credits: 84 },
  cancellation_requested: false,
  error_code: null,
  error_message: null,
  started_at: "2026-08-16T12:00:00Z",
  completed_at: null,
  created_at: "2026-08-16T12:00:00Z",
  updated_at: "2026-08-16T12:03:00Z",
}

const baseCandidate = {
  job_id: jobId,
  current_title: "Senior Product Manager",
  current_company: "PayFlow",
  location: "New York, United States",
  classification: "main",
  scorecard_version_id: scorecardId,
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

export const previewPriya: JobCandidate = {
  ...baseCandidate,
  id: "00000000-0000-4000-8000-000000000501",
  candidate_id: "00000000-0000-4000-8000-000000000601",
  run_candidate_id: "00000000-0000-4000-8000-000000000611",
  full_name: "Priya Sharma",
  score: 92,
  contacts: [{
    id: "00000000-0000-4000-8000-000000000701",
    kind: "email",
    classification: "work",
    masked_value: "p••••@••••.test",
    verification_state: "verified",
    expires_at: "2026-09-15T00:00:00Z",
  }],
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
      { key: "payments", label: "Payments experience", state: "supported", summary: "5 years in payments and fintech", points: 20, max_points: 20 },
      { key: "us_market", label: "US market experience", state: "unknown", summary: "US market experience is unknown", points: 0, max_points: 5 },
    ],
    failed_must_haves: [],
    unknown_keys: ["us_market"],
  },
}

export const previewMarcus: JobCandidate = {
  ...baseCandidate,
  id: "00000000-0000-4000-8000-000000000502",
  candidate_id: "00000000-0000-4000-8000-000000000602",
  run_candidate_id: "00000000-0000-4000-8000-000000000612",
  full_name: "Marcus Lee",
  current_title: "Product Lead",
  current_company: "LedgerWorks",
  score: 84,
  contacts: [],
  has_contact: false,
  enrichment_eligible: true,
  estimated_enrichment_credits: 2,
  score_json: { total: 84, breakdown: {}, criteria: [], failed_must_haves: [], unknown_keys: [] },
}

export const previewNearMatch: JobCandidate = {
  ...baseCandidate,
  id: "00000000-0000-4000-8000-000000000503",
  candidate_id: "00000000-0000-4000-8000-000000000603",
  run_candidate_id: "00000000-0000-4000-8000-000000000613",
  full_name: "Avery Stone",
  classification: "near_match",
  score: 68,
  has_contact: false,
  contacts: [],
  mandatory_gaps: [
    { key: "payments", label: "Payments experience", state: "failed", summary: "Missing required payments experience" },
    { key: "work_eligibility", label: "Work eligibility", state: "unknown", summary: "Work eligibility is unknown" },
  ],
  score_json: null,
}

const immutableScorecard: ConfirmedScorecard = {
  id: scorecardId,
  job_id: jobId,
  version: 3,
  target_titles: ["Senior Product Manager"],
  criteria: previewDraft.draft.criteria ?? [],
  seniority: ["senior"],
  minimum_years: 5,
  maximum_years: null,
  locations: ["New York, NY"],
  industry_code: "technology.fintech",
  suggested_adjacent_industries: [],
  uncertainties: [],
  confirmed_inferred_items: [],
  extraction_status: "ready",
  confirmed_at: "2026-08-16T11:59:00Z",
}

type PreviewPhase = "intake" | "scorecard" | "workspace"

export function Task13Preview({ startSourcing }: { startSourcing: boolean }) {
  const [phase, setPhase] = useState<PreviewPhase>(startSourcing ? "workspace" : "intake")
  const [draft, setDraft] = useState<ScorecardDraftResponse>(previewDraft)
  const [run, setRun] = useState<SourcingRun>(previewRun)

  if (phase === "intake") {
    return (
      <section className="page-stack" aria-labelledby="preview-intake-heading">
        <header className="page-header"><div><p className="eyebrow">Deterministic development preview</p><h1 id="preview-intake-heading">Create sourcing brief</h1><p>Exercise the production intake and scorecard components with intercepted test APIs.</p></div></header>
        <JobIntakeForm clients={[client]} onDraftReady={(generated) => {
          setDraft(generated)
          setPhase("scorecard")
        }} />
      </section>
    )
  }

  if (phase === "scorecard") {
    return (
      <section className="page-stack" aria-labelledby="preview-scorecard-heading">
        <header className="page-header"><div><p className="eyebrow">Confirm before sourcing</p><h1 id="preview-scorecard-heading">Review scorecard</h1><p>The production editor owns save, confirmation, and sourcing intents.</p></div></header>
        <ScorecardEditor
          draft={draft}
          allowedIndustryCodes={client.industry_codes}
          onStarted={(startedRun) => {
            setRun(startedRun)
            setPhase("workspace")
          }}
        />
      </section>
    )
  }

  return (
    <section className="page-stack workspace-page" aria-labelledby="preview-job-heading">
      <header className="page-header workspace-header"><div><p className="eyebrow">Candidate review</p><h1 id="preview-job-heading">Senior Product Manager</h1><p>Production components with deterministic dev/test-only data.</p></div></header>
      <RunStatus jobId={jobId} initialRun={run} />
      <ReviewWorkspace
        jobId={jobId}
        runId={run.id}
        initialCandidates={{ items: [previewPriya, previewMarcus], next_cursor: null }}
        initialSelectedCandidate={previewPriya}
        initialNearMatches={{ items: [previewNearMatch], next_cursor: null }}
        immutableScorecard={immutableScorecard}
        ownerOptions={[{ userId: "00000000-0000-4000-8000-000000000802", name: "Avery Stone" }]}
      />
    </section>
  )
}

export function Task13DirectoryPreview() {
  return (
    <section className="page-stack" aria-labelledby="preview-directory-heading">
      <header className="page-header"><div><p className="eyebrow">Talent directory</p><h1 id="preview-directory-heading">Candidates</h1><p>Only authorized canonical people and job history are returned.</p></div></header>
      <CandidateDirectory initialPage={{
        items: [{
          id: previewPriya.candidate_id,
          name: previewPriya.full_name,
          current_title: previewPriya.current_title,
          current_company: previewPriya.current_company,
          location: previewPriya.location,
          industry_codes: ["technology.fintech"],
          job_ids: [jobId],
          updated_at: previewPriya.updated_at,
        }],
        next_cursor: null,
      }} />
    </section>
  )
}

const alert: Notification = {
  id: "00000000-0000-4000-8000-000000000901",
  code: "usage_budget_exhausted",
  title: "Sourcing budget reached",
  message: "The configured sourcing budget was reached.",
  run_id: runId,
  acknowledged_at: null,
  created_at: "2026-08-16T12:04:00Z",
}
const member: Member = {
  membership_id: "00000000-0000-4000-8000-000000000801",
  user_id: "00000000-0000-4000-8000-000000000802",
  email: "recruiter@example.test",
  display_name: "Preview Recruiter",
  role: "recruiter",
  allowed_client_ids: [clientId],
  active: true,
}

export function Task13SettingsPreview({ role }: { role: Role }) {
  return (
    <section className="page-stack" aria-labelledby="preview-settings-heading">
      <header className="page-header"><div><p className="eyebrow">Agency controls</p><h1 id="preview-settings-heading">Settings</h1></div></header>
      <AgencyAlerts alerts={[alert]} />
      <MembershipManager role={role} members={[member]} clients={[client]} />
    </section>
  )
}
