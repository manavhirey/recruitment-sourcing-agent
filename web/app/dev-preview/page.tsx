import { notFound } from "next/navigation"

import { AppShell } from "@/components/layout/AppShell"
import { QueryProvider } from "@/components/layout/QueryProvider"
import { ScorecardEditor } from "@/components/scorecards/ScorecardEditor"
import type { ScorecardDraftResponse } from "@/lib/schemas"

const tenantId = "00000000-0000-4000-8000-000000000001"
const jobId = "00000000-0000-4000-8000-000000000101"

const previewDraft: ScorecardDraftResponse = {
  job_id: jobId,
  draft_revision: 2,
  original_job_description: "Synthetic preview brief.",
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
}

export default async function DevelopmentPreview({
  searchParams,
}: {
  searchParams: Promise<{ view?: string; state?: string }>
}) {
  if (process.env.NODE_ENV === "production" || process.env.ENABLE_DEV_PREVIEW !== "true") {
    notFound()
  }
  const params = await searchParams
  const scorecard = params.view === "scorecard"
  const task13 = params.view === "task13"
  const task13Directory = params.view === "task13-directory"
  const task13Settings = params.view === "task13-settings"
  const task13Preview = task13 || task13Directory || task13Settings
    ? await import("@/components/dev/Task13Preview")
    : null
  const content = task13 && task13Preview
    ? <task13Preview.Task13Preview startSourcing={params.state === "sourcing"} />
    : task13Directory && task13Preview
      ? <task13Preview.Task13DirectoryPreview />
      : task13Settings && task13Preview
        ? <task13Preview.Task13SettingsPreview role={params.state === "recruiter" ? "recruiter" : "owner"} />
        : <div className="page-stack">
          <header className="page-header">
            <div>
              <p className="eyebrow">Development preview</p>
              <h1>{scorecard ? "Review scorecard" : "Jobs"}</h1>
              <p>Deterministic, non-production visual QA data.</p>
            </div>
          </header>
          {scorecard ? (
            <ScorecardEditor
              draft={previewDraft}
              allowedIndustryCodes={["technology.fintech"]}
            />
          ) : (
            <div className="empty-state">
              <p className="eyebrow">Job pipeline</p>
              <h2>Build a defensible shortlist from a clear brief.</h2>
              <p>Review every inferred criterion before starting evidence-led sourcing.</p>
              <a className="button button-primary" href="/dev-preview?view=scorecard">
                Review preview scorecard
              </a>
            </div>
          )}
        </div>
  return (
    <QueryProvider>
      <AppShell
        agency={{ id: tenantId, name: "Northstar Search" }}
        user={{ name: "Avery Stone", email: "avery@example.test" }}
        role="owner"
        tenantOptions={[
          { id: tenantId, name: "Northstar Search" },
          {
            id: "00000000-0000-4000-8000-000000000002",
            name: "Harbor Recruiting",
          },
        ]}
        activeJobs={[
          { id: jobId, title: "Senior Product Manager", status: "awaiting_scorecard" },
          {
            id: "00000000-0000-4000-8000-000000000102",
            title: "Director of Partnerships",
            status: "sourcing",
          },
        ]}
      >
        {content}
      </AppShell>
    </QueryProvider>
  )
}
