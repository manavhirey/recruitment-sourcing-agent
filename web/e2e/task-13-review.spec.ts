import path from "node:path"
import { createHmac } from "node:crypto"

import { expect, test, type BrowserContext, type Page } from "@playwright/test"
import { encode } from "next-auth/jwt"

import { seniorityOptionsFixture } from "@/tests/fixtures"

const evidenceDirectory = path.resolve(
  "../.superpowers/sdd/2026-08-15-recruitment-sourcing-agent-vertical-slice",
)
const tenantId = "00000000-0000-4000-8000-000000000001"
const clientId = "00000000-0000-4000-8000-000000000201"
const jobId = "00000000-0000-4000-8000-000000000101"
const runId = "00000000-0000-4000-8000-000000000301"
const scorecardId = "00000000-0000-4000-8000-000000000403"
const marcusId = "00000000-0000-4000-8000-000000000502"
const authSecret = "x8V1qM3rT6yB9nC2pL5sF7hJ0kD4wZ6aQ8eR1tY3uI5oP7gH"
const jobTitle = "Senior Product Manager"
const extractedJobDescription = "Senior Product Designer\nLead product design for the growth team."

type ObservedBffRequest = {
  method: string
  path: string
}

function observeBffRequests(page: Page): ObservedBffRequest[] {
  const observed: ObservedBffRequest[] = []
  page.on("request", (request) => {
    const { pathname } = new URL(request.url())
    if (pathname.startsWith("/api/bff/")) {
      observed.push({ method: request.method(), path: pathname })
    }
  })
  return observed
}

const draft = {
  job_id: jobId,
  draft_revision: 2,
  original_job_description: "Lead a payments platform and product-led growth strategy.",
  extraction_status: "ready",
  extraction_warning: null,
  seniority_options: seniorityOptionsFixture,
  draft: {
    target_titles: ["Senior Product Manager"],
    criteria: [
      { key: "payments", label: "Payments platform experience", kind: "must_have", evidence_required: true, source_text: "payments platform experience", inferred: false, recruiter_entered: false, lawful_requirement_confirmed: false },
      { key: "growth", label: "Led product-led growth", kind: "preference", evidence_required: false, source_text: "product-led growth strategy", inferred: false, recruiter_entered: false, lawful_requirement_confirmed: false },
    ],
    seniority: ["senior"],
    minimum_years: null,
    maximum_years: null,
    locations: ["New York, NY"],
    industry_code: "technology.fintech",
    suggested_adjacent_industries: [],
    uncertainties: [],
    confirmed_inferred_items: [],
  },
}

function run(state: "partially_ready" | "cancelled" = "partially_ready") {
  return {
    id: runId,
    tenant_id: tenantId,
    job_id: jobId,
    scorecard_version_id: scorecardId,
    state,
    current_stage: state === "cancelled" ? "cancelled" : "enrichment",
    candidate_count: 126,
    matched_count: 18,
    enriched_count: 12,
    failed_count: 1,
    budget_use: { estimated_credits: 84 },
    cancellation_requested: state === "cancelled",
    error_code: null,
    error_message: null,
    started_at: "2026-08-16T12:00:00Z",
    completed_at: state === "cancelled" ? "2026-08-16T12:05:00Z" : null,
    created_at: "2026-08-16T12:00:00Z",
    updated_at: "2026-08-16T12:05:00Z",
  }
}

const marcus = {
  id: marcusId,
  job_id: jobId,
  candidate_id: "00000000-0000-4000-8000-000000000602",
  run_candidate_id: "00000000-0000-4000-8000-000000000612",
  full_name: "Marcus Lee",
  current_title: "Product Lead",
  current_company: "LedgerWorks",
  location: "New York, United States",
  classification: "main",
  score: 84,
  score_json: { total: 84, breakdown: {}, criteria: [], failed_must_haves: [], unknown_keys: [] },
  scorecard_version_id: scorecardId,
  scorecard_version: 3,
  scoring_version: "matching-v1",
  stage: "New",
  owner_user_id: null,
  rejection_reason_code: null,
  rejection_note: null,
  tags: ["Payments"],
  has_contact: false,
  enrichment_eligible: true,
  estimated_enrichment_credits: 2,
  mandatory_gaps: [],
  contacts: [],
  experiences: [],
  provenance: [],
  notes: [],
  created_at: "2026-08-15T00:00:00Z",
  updated_at: "2026-08-16T00:00:00Z",
}

async function interceptProductionBff(page: Page) {
  let currentRun = run()
  await page.route("**/api/bff/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const { pathname } = url
    const method = request.method()
    const json = async (body: unknown, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      headers: { "Cache-Control": "private, no-store" },
      body: JSON.stringify(body),
    })

    if (pathname.endsWith("/job-descriptions/extract") && method === "POST") {
      const multipart = request.postDataBuffer()
      if (multipart?.includes(Buffer.from('filename="encrypted.pdf"'))) {
        await json({ code: "job_description_file_unreadable" }, 422)
        return
      }
      if (multipart?.includes(Buffer.from('filename="scan.pdf"'))) {
        await json({ code: "job_description_text_missing" }, 422)
        return
      }
      const sourceFilename = multipart?.includes(Buffer.from('filename="role.docx"'))
        ? "role.docx"
        : "role.pdf"
      await json({
        text: extractedJobDescription,
        source: {
          filename: sourceFilename,
          media_type: sourceFilename.endsWith(".docx")
            ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            : "application/pdf",
        },
      })
      return
    }
    if (pathname === "/api/bff/jobs" && method === "POST") {
      await json({
        id: jobId,
        tenant_id: tenantId,
        client_id: clientId,
        owner_user_id: "00000000-0000-4000-8000-000000000802",
        title: "Senior Product Manager",
        job_description: "Lead a payments platform and product-led growth strategy.",
        location: "New York, NY",
        employment_model: "hybrid",
        status: "awaiting_scorecard",
        draft_revision: 1,
        extraction_status: "ready",
        extraction_warning: null,
        current_scorecard_id: null,
        created_at: "2026-08-16T11:55:00Z",
        updated_at: "2026-08-16T11:55:00Z",
      })
      return
    }
    if (pathname.endsWith("/scorecard/generate") && method === "POST") {
      await json(draft)
      return
    }
    if (pathname.endsWith("/scorecard/draft") && method === "PUT") {
      await json({ ...draft, draft_revision: 3 })
      return
    }
    if (pathname.endsWith("/scorecard/confirm") && method === "POST") {
      await json({ id: scorecardId, job_id: jobId, version: 3, ...draft.draft, extraction_status: "ready", confirmed_at: "2026-08-16T11:59:00Z" })
      return
    }
    if (pathname === `/api/bff/jobs/${jobId}/runs` && method === "POST") {
      await json(currentRun)
      return
    }
    if (pathname === `/api/bff/runs/${runId}` && method === "GET") {
      await json(currentRun)
      return
    }
    if (pathname === `/api/bff/runs/${runId}/cancel` && method === "POST") {
      currentRun = run("cancelled")
      await json(currentRun)
      return
    }
    if (pathname === `/api/bff/job-candidates/${marcusId}` && method === "GET") {
      await json(marcus)
      return
    }
    if (pathname.endsWith("/stage") && method === "PATCH") {
      await json({})
      return
    }
    if (pathname.endsWith("/reveal") && method === "POST") {
      await json({ id: "00000000-0000-4000-8000-000000000701", value: "priya@example.test" })
      return
    }
    if (pathname === "/api/bff/candidates" && method === "GET") {
      await json({ items: [], next_cursor: null })
      return
    }
    if (pathname.startsWith("/api/bff/notifications/") && method === "PATCH") {
      await json({
        id: "00000000-0000-4000-8000-000000000901",
        code: "usage_budget_exhausted",
        title: "Sourcing budget reached",
        message: "The configured sourcing budget was reached.",
        run_id: runId,
        acknowledged_at: "2026-08-16T12:06:00Z",
        created_at: "2026-08-16T12:04:00Z",
      })
      return
    }
    await json({ code: "unexpected_test_request" }, 500)
  })
}

function tenantSelectionCookie() {
  const expires = Math.floor(Date.now() / 1_000) + 7 * 24 * 60 * 60
  const payload = `v1.${tenantId}.${expires}`
  const signingKey = createHmac("sha256", authSecret)
    .update("recruitment-sourcing:selected-tenant:v1")
    .digest()
  const signature = createHmac("sha256", signingKey)
    .update(payload)
    .digest("base64url")
  return `${payload}.${signature}`
}

async function authenticateRealRoutes(context: BrowserContext) {
  const sessionToken = await encode({
    secret: authSecret,
    salt: "next-auth.session-token",
    maxAge: 60 * 60,
    token: {
      sub: "oidc|e2e-owner",
      name: "E2E Owner",
      email: "owner@example.test",
      providerAccessToken: "e2e-access-token",
      providerExpiresAt: Math.floor(Date.now() / 1_000) + 60 * 60,
      tenantOptions: [{ id: tenantId, name: "E2E Agency" }],
    },
  })
  await context.addCookies([
    {
      name: "next-auth.session-token",
      value: sessionToken,
      url: "http://127.0.0.1:3000",
      httpOnly: true,
      sameSite: "Lax",
    },
    {
      name: "sourcing-tenant",
      value: tenantSelectionCookie(),
      url: "http://127.0.0.1:3000",
      httpOnly: true,
      sameSite: "Strict",
    },
  ])
}

test.beforeEach(async ({ page }, testInfo) => {
  if (!testInfo.title.includes("real authenticated route")) {
    await interceptProductionBff(page)
  }
})

test("real authenticated route crosses Next BFF and deterministic FastAPI", async ({
  context,
  page,
  request,
}) => {
  await authenticateRealRoutes(context)
  const beforeResponse = await request.get(
    "http://127.0.0.1:8001/__e2e__/observed",
  )
  const observedBefore = (await beforeResponse.json() as Array<Record<string, string>>).length
  await page.goto("/jobs/new")

  await expect(page.getByRole("heading", { level: 1, name: "Turn the brief into a scorecard" })).toBeVisible()
  await page.getByLabel("Job title").fill(jobTitle)
  await page.getByRole("textbox", { name: "Job description" }).fill("Lead a payments platform and product-led growth strategy.")
  await page.getByLabel("Location").fill("New York, NY")
  await page.getByLabel("Employment model").selectOption("hybrid")
  await page.getByLabel("Client").selectOption(clientId)
  await expect(page.getByLabel("Client")).toHaveValue(clientId)
  await page.getByRole("button", { name: "Generate scorecard" }).click()

  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]{36}\/scorecard$/)
  const createdJobId = new URL(page.url()).pathname.split("/")[2]
  expect(createdJobId).toMatch(/^[0-9a-f-]{36}$/)
  await expect(page.getByRole("heading", { level: 1, name: "Senior Product Manager" })).toBeVisible()
  await page.getByRole("button", { name: "Confirm and source" }).click()
  await expect(page).toHaveURL(/\/jobs$/)

  await page.goto(`/jobs/${createdJobId}`)
  await expect(page.getByRole("heading", { level: 1, name: "Senior Product Manager" })).toBeVisible()
  await expect(page.getByRole("button", { name: /Priya Sharma.*92/ })).toBeVisible()
  await page.getByRole("button", { name: "Shortlist" }).click()
  await expect(page.locator(".stage-pill")).toHaveText("Shortlisted")

  await expect.poll(async () => {
    const response = await request.get("http://127.0.0.1:8001/__e2e__/observed")
    const entries = (await response.json() as Array<Record<string, string>>)
      .slice(observedBefore)
    return entries.some(
      (entry) =>
        /^\/api\/v1\/job-candidates\/[0-9a-f-]{36}\/stage$/.test(entry.path),
    )
  }).toBe(true)
  const observedResponse = await request.get(
    "http://127.0.0.1:8001/__e2e__/observed",
  )
  const observed = (await observedResponse.json() as Array<Record<string, string>>)
    .slice(observedBefore)
  expect(observed.some((entry) => entry.path === "/api/v1/jobs" && entry.method === "POST")).toBe(true)
  expect(observed.some((entry) => entry.path === `/api/v1/jobs/${createdJobId}/scorecard/generate`)).toBe(true)
  expect(observed.every((entry) => entry.authorization === "present" && entry.tenant === tenantId)).toBe(true)
})

test("real invitation fragment is removed before the first server request", async ({
  context,
  page,
}) => {
  const invitationToken = `${tenantId}.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq`
  const capturePromise = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/invite/capture",
  )

  await page.goto(`/invite#${invitationToken}`, { waitUntil: "domcontentloaded" })
  const capture = await capturePromise

  expect(capture.url()).not.toContain(invitationToken)
  expect(capture.headers().referer ?? "").not.toContain(invitationToken)
  expect(capture.postDataJSON()).toEqual({ token: invitationToken })
  await expect.poll(() => page.url()).not.toContain(invitationToken)
  expect(JSON.stringify(await context.cookies())).not.toContain(invitationToken)
  await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => null)
  expect(page.url()).not.toContain(invitationToken)
})

for (const file of [
  { name: "role.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4") },
  {
    name: "role.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("PK\x03\x04"),
  },
]) {
  test(`uploaded ${file.name} populates editable text before generation`, async ({ page }) => {
    const observed = observeBffRequests(page)
    await page.goto("/dev-preview?view=task13")

    await page.getByLabel("Upload job description", { exact: true }).setInputFiles(file)
    const description = page.getByRole("textbox", { name: "Job description" })
    await expect(description).toHaveValue(extractedJobDescription)
    await description.fill("Senior Product Designer\nLead growth product design.")
    await page.getByLabel("Client").selectOption(clientId)
    await page.getByLabel("Job title").fill(jobTitle)
    await page.getByRole("button", { name: "Generate scorecard" }).click()

    await expect(page.getByRole("heading", { level: 1, name: "Review scorecard" })).toBeVisible()
    expect(observed).toEqual([
      { method: "POST", path: "/api/bff/job-descriptions/extract" },
      { method: "POST", path: "/api/bff/jobs" },
      { method: "POST", path: `/api/bff/jobs/${jobId}/scorecard/generate` },
    ])
    expect(JSON.stringify(observed)).not.toContain(file.name)
    expect(JSON.stringify(observed)).not.toContain(file.buffer.toString())
  })
}

test("cancelling document replacement keeps reviewed text and skips extraction", async ({ page }) => {
  const observed = observeBffRequests(page)
  await page.goto("/dev-preview?view=task13")
  const description = page.getByRole("textbox", { name: "Job description" })
  await description.fill("Keep this reviewed job description.")

  await page.getByLabel("Upload job description", { exact: true }).setInputFiles({
    name: "replacement.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4"),
  })
  await expect(page.getByRole("dialog", { name: "Replace job description?" })).toBeVisible()
  await page.getByRole("button", { name: "Keep existing text" }).click()

  await expect(description).toHaveValue("Keep this reviewed job description.")
  await expect(page.getByRole("dialog", { name: "Replace job description?" })).toBeHidden()
  expect(observed).toEqual([])
})

test("selected early and mid-level presets remain visible under a minimum-only override", async ({ page }) => {
  await page.goto("/dev-preview?view=task13")
  await page.getByLabel("Client").selectOption(clientId)
  await page.getByLabel("Job title").fill(jobTitle)
  await page.getByRole("textbox", { name: "Job description" }).fill(
    "Lead a payments platform and product-led growth strategy.",
  )
  await page.getByRole("button", { name: "Generate scorecard" }).click()
  await expect(page.getByRole("heading", { level: 1, name: "Review scorecard" })).toBeVisible()

  const earlyCareer = page.getByRole("checkbox", { name: /Early-Career/ })
  const midLevel = page.getByRole("checkbox", { name: /Mid-Level/ })
  const senior = page.getByRole("checkbox", { name: /^Senior/ })
  await senior.uncheck()
  await earlyCareer.check()
  await midLevel.check()
  await page.getByRole("checkbox", { name: "Use custom experience range" }).check()
  await page.getByLabel("Minimum years").fill("4")

  await expect(earlyCareer).toBeChecked()
  await expect(earlyCareer).toBeDisabled()
  await expect(midLevel).toBeChecked()
  await expect(midLevel).toBeDisabled()
  await expect(page.getByLabel("Minimum years")).toHaveValue("4")
  await expect(page.getByLabel("Maximum years")).toHaveValue("")
  await expect(page.getByText("This custom range overrides the selected seniority levels.")).toBeVisible()
})

for (const failure of [
  {
    name: "encrypted.pdf",
    code: "job_description_file_unreadable",
    message: "The uploaded job description file is corrupted or might be password-protected.",
  },
  {
    name: "scan.pdf",
    code: "job_description_text_missing",
    message: "No readable text was found. Upload a text-based document or paste the job description.",
  },
]) {
  test(`${failure.code} stops before OCR or job creation`, async ({ page }) => {
    const observed = observeBffRequests(page)
    await page.goto("/dev-preview?view=task13")

    await page.getByLabel("Upload job description", { exact: true }).setInputFiles({
      name: failure.name,
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4"),
    })

    await expect(page.locator(".upload-error [role='alert']")).toHaveText(failure.message)
    expect(observed).toEqual([
      { method: "POST", path: "/api/bff/job-descriptions/extract" },
    ])
    expect(JSON.stringify(observed)).not.toContain(failure.name)
    expect(JSON.stringify(observed)).not.toContain("%PDF-1.4")
  })
}

test("production intake, scorecard, run, review, and shortlist components work together", async ({ page }, testInfo) => {
  await page.goto("/dev-preview?view=task13")
  await page.waitForLoadState("networkidle")

  await expect(page.getByRole("heading", { level: 1, name: "Create sourcing brief" })).toBeVisible()
  await page.getByLabel("Client").selectOption(clientId)
  await page.getByLabel("Job title").fill(jobTitle)
  await page.getByRole("textbox", { name: "Job description" }).fill("Lead a payments platform and product-led growth strategy.")
  await page.getByLabel("Location").fill("New York, NY")
  await page.getByLabel("Employment model").selectOption("hybrid")
  await page.getByRole("button", { name: "Generate scorecard" }).click()
  await expect(page.getByRole("heading", { level: 1, name: "Review scorecard" })).toBeVisible()
  await page.getByRole("button", { name: "Confirm and source" }).click()
  await expect(page.getByRole("heading", { level: 2, name: "partially ready" })).toBeVisible()
  await expect(page.getByRole("button", { name: /Priya Sharma.*92/ })).toBeVisible()
  await expect(page.getByText("US market experience is unknown")).toBeVisible()
  await page.getByRole("button", { name: "Shortlist" }).click()
  await expect(page.locator(".stage-pill")).toHaveText("Shortlisted")
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({
    path: path.join(evidenceDirectory, `task-13-workspace-${testInfo.project.name}.png`),
    fullPage: true,
  })
})

test("production cancellation retains candidates and mandatory gaps precede score", async ({ page }) => {
  await page.goto("/dev-preview?view=task13&state=sourcing")
  await page.getByRole("button", { name: "Cancel sourcing" }).click()
  await expect(page.getByRole("dialog", { name: "Cancel sourcing run?" })).toBeVisible()
  await page.getByRole("button", { name: "Confirm cancellation" }).click()
  await expect(page.getByRole("heading", { level: 2, name: "cancelled" })).toBeVisible()
  await expect(page.getByRole("button", { name: /Priya Sharma.*92/ })).toBeVisible()
  await page.getByRole("tab", { name: "Near Matches" }).click()
  const card = page.getByRole("listitem").filter({ hasText: "Avery Stone" })
  const text = await card.innerText()
  expect(text.indexOf("Missing required payments experience")).toBeLessThan(text.indexOf("Work eligibility is unknown"))
  expect(text.indexOf("Work eligibility is unknown")).toBeLessThan(text.indexOf("68 / 100"))
})

test("production contact reveal is ephemeral and directory is non-disclosing", async ({ page }) => {
  await page.goto("/dev-preview?view=task13&state=sourcing")
  await page.getByRole("button", { name: "Reveal work email" }).click()
  await expect(page.getByText("priya@example.test")).toBeVisible()
  expect(page.url()).not.toContain("priya")
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
  await page.getByRole("button", { name: /Marcus Lee.*84/ }).click()
  await expect(page.getByText("priya@example.test")).toBeHidden()

  await page.goto("/dev-preview?view=task13-directory")
  await page.getByLabel("Search candidates").fill("outside tenant")
  await page.getByRole("button", { name: "Search" }).click()
  await expect(page.getByText("No authorized matches")).toBeVisible()
  expect(page.url()).not.toContain("outside")
})

test("production alerts and membership controls respect role", async ({ page }) => {
  await page.goto("/dev-preview?view=task13-settings")
  await expect(page.getByRole("heading", { name: "Unread alerts" })).toBeVisible()
  await expect(page.getByRole("button", { name: "Create invitation" })).toBeVisible()
  await page.getByRole("button", { name: "Acknowledge" }).click()
  await expect(page.getByText("No unread tenant alerts.")).toBeVisible()

  await page.goto("/dev-preview?view=task13-settings&state=recruiter")
  await expect(page.getByText("Only agency owners and admins can manage membership.")).toBeVisible()
  await expect(page.getByRole("button", { name: "Create invitation" })).toBeHidden()
})
