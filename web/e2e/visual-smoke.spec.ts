import path from "node:path"

import { expect, test } from "@playwright/test"

const evidenceDirectory = path.resolve(
  "../.superpowers/sdd/2026-08-15-recruitment-sourcing-agent-vertical-slice",
)

test("agency shell is keyboard accessible and responsive", async ({ page }, testInfo) => {
  await page.goto("/dev-preview")

  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible()
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible()
  await expect(page.getByLabel("Agency")).toBeVisible()
  await page.keyboard.press("Tab")
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused()
  await page.keyboard.press("Enter")
  await expect(page.locator("#main-content")).toBeFocused()
  if (testInfo.project.name.startsWith("mobile")) {
    await expect(page.getByRole("complementary", { name: "Active jobs" })).toBeHidden()
  } else {
    await expect(page.getByRole("complementary", { name: "Active jobs" })).toBeVisible()
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  const contrastRatios = await page.locator(".brand-mark, .button-primary").evaluateAll((elements) => {
    const luminance = (color: string) => {
      const channels = color.match(/[\d.]+/g)?.slice(0, 3).map(Number) ?? []
      const linear = channels.map((channel) => {
        const normalized = channel / 255
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4
      })
      return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    }
    return elements.map((element) => {
      const style = getComputedStyle(element)
      const foreground = luminance(style.color)
      const background = luminance(style.backgroundColor)
      return (Math.max(foreground, background) + 0.05) /
        (Math.min(foreground, background) + 0.05)
    })
  })
  expect(Math.min(...contrastRatios)).toBeGreaterThanOrEqual(4.5)
  await page.screenshot({
    path: path.join(evidenceDirectory, `task-12-shell-${testInfo.project.name}.png`),
    fullPage: true,
  })
})

test("scorecard review remains usable without horizontal overflow", async ({ page }, testInfo) => {
  await page.goto("/dev-preview?view=scorecard")

  await expect(page.getByRole("heading", { level: 1, name: "Review scorecard" })).toBeVisible()
  await expect(page.getByRole("button", { name: "Confirm and source" })).toBeDisabled()
  await expect(page.getByText("Suggested — confirm before use")).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({
    path: path.join(evidenceDirectory, `task-12-scorecard-${testInfo.project.name}.png`),
    fullPage: true,
  })
})
