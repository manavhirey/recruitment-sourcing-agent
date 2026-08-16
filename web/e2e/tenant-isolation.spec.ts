import { expect, test } from "@playwright/test"

const ownedTenant = "00000000-0000-4000-8000-000000000001"
const unownedTenant = "00000000-0000-4000-8000-000000000999"

test("an authenticated token cannot select another tenant", async ({ request }) => {
  const response = await request.get("http://127.0.0.1:8001/api/v1/jobs", {
    headers: {
      Authorization: "Bearer e2e-access-token",
      "X-Tenant-ID": unownedTenant,
    },
  })

  expect(response.status()).toBe(404)
  expect(await response.text()).not.toContain("Northstar Payments")
})
test("the same token can access only its owned tenant", async ({ request }) => {
  const response = await request.get("http://127.0.0.1:8001/api/v1/jobs", {
    headers: {
      Authorization: "Bearer e2e-access-token",
      "X-Tenant-ID": ownedTenant,
    },
  })

  expect(response.status()).toBe(200)
  const body = await response.json() as { items: Array<{ id: string }> }
  expect(body.items).toEqual(expect.any(Array))
})
