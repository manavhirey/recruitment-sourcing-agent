import { expect, test } from "@playwright/test"

const clientId = "00000000-0000-4000-8000-000000000201"

test("developer override reaches the real BFF without company sign-in", async ({
  page,
  request,
}) => {
  const observedBefore = await request.get(
    "http://127.0.0.1:8001/__e2e__/observed",
  )
  const beforeCount = (
    (await observedBefore.json()) as Array<Record<string, string>>
  ).length
  await page.goto("/dev-preview?view=task13")
  await page.waitForLoadState("networkidle")

  await page.getByLabel("Client").selectOption(clientId)
  await page.getByLabel("Job title").fill("Senior Product Manager")
  await page.getByLabel("Job description").fill(
    "Lead a payments platform and product-led growth strategy.",
  )
  await page.getByLabel("Location").fill("New York, NY")
  await page.getByLabel("Employment model").selectOption("hybrid")
  await page.getByRole("button", { name: "Generate scorecard" }).click()

  await expect(page).toHaveURL("/dev-preview?view=task13")
  await expect(
    page.getByRole("heading", { level: 1, name: "Review scorecard" }),
  ).toBeVisible()

  const observedAfter = await request.get(
    "http://127.0.0.1:8001/__e2e__/observed",
  )
  const newRequests = (
    (await observedAfter.json()) as Array<Record<string, string>>
  ).slice(beforeCount)
  expect(
    newRequests.some(
      (entry) => entry.method === "POST" && entry.path === "/api/v1/jobs",
    ),
  ).toBe(true)
  expect(newRequests.every((entry) => entry.authorization === "present")).toBe(true)
  expect(
    newRequests.every(
      (entry) => entry.tenant === "00000000-0000-4000-8000-000000000001",
    ),
  ).toBe(true)
})
