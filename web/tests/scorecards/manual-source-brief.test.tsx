import { render, screen } from "@testing-library/react"

import { ManualSourceBrief } from "@/components/scorecards/ManualSourceBrief"

describe("manual scorecard source brief", () => {
  it("renders the authorized original job description as read-only context", () => {
    render(
      <ManualSourceBrief jobDescription="Build payment operations for the Northeast team." />,
    )

    expect(screen.getByText("Review original job description")).toBeVisible()
    expect(
      screen.getByText("Build payment operations for the Northeast team."),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("textbox", { name: /job description/i }),
    ).not.toBeInTheDocument()
  })
})
