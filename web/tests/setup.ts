import "@testing-library/jest-dom/vitest"

import { cleanup } from "@testing-library/react"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, beforeEach, vi } from "vitest"

const navigationMocks = vi.hoisted(() => ({
  push: vi.fn(),
  refresh: vi.fn(),
  replace: vi.fn(),
}))

export { navigationMocks }

vi.mock("next/navigation", () => ({
  usePathname: () => "/jobs",
  useRouter: () => navigationMocks,
}))

export const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
beforeEach(() => window.history.replaceState({}, "", "/jobs"))
afterEach(() => {
  cleanup()
  server.resetHandlers()
  navigationMocks.push.mockReset()
  navigationMocks.refresh.mockReset()
  navigationMocks.replace.mockReset()
})
afterAll(() => server.close())
