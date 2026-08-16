import { render } from "@testing-library/react"
import axe from "axe-core"

import { ClientManager } from "@/components/clients/ClientManager"
import { JobIntakeForm } from "@/components/jobs/JobIntakeForm"
import { AppShell } from "@/components/layout/AppShell"
import { ScorecardEditor } from "@/components/scorecards/ScorecardEditor"
import { authorizedClientsFixture, scorecardDraftFixture } from "@/tests/fixtures"

async function expectNoAxeViolations(container: HTMLElement) {
  const result = await axe.run(container, {
    rules: {
      // jsdom has no layout engine, so color contrast is covered by browser QA.
      "color-contrast": { enabled: false },
    },
  })
  expect(result.violations).toEqual([])
}

describe("core flow accessibility", () => {
  it("keeps the responsive agency shell and intake form free of axe violations", async () => {
    const { container } = render(
      <AppShell
        agency={{ id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" }}
        user={{ name: "Avery Stone", email: "avery@example.test" }}
        role="recruiter"
        tenantOptions={[
          { id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" },
        ]}
        activeJobs={[]}
      >
        <h1>New job</h1>
        <JobIntakeForm clients={authorizedClientsFixture} />
      </AppShell>,
    )

    await expectNoAxeViolations(container)
  })

  it("keeps scorecard review semantics free of axe violations", async () => {
    const { container } = render(
      <ScorecardEditor
        draft={scorecardDraftFixture}
        allowedIndustryCodes={["technology.fintech"]}
      />,
    )

    await expectNoAxeViolations(container)
  })

  it("keeps recruiter and manager client views free of axe violations", async () => {
    const { container } = render(
      <main>
        <h1>Clients</h1>
        <ClientManager clients={authorizedClientsFixture} role="admin" />
      </main>,
    )

    await expectNoAxeViolations(container)
  })
})
