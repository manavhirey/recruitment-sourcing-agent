import { expect, test } from "@playwright/test"

test("production compiles the development preview to 404", async ({ page }) => {
  const response = await page.goto("/dev-preview?view=task13")

  expect(response?.status()).toBe(404)
  await expect(page.getByText("This page could not be found.")).toBeVisible()
})
