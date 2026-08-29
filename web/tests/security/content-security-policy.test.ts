import { contentSecurityPolicyFor } from "@/next.config"

describe("content security policy", () => {
  it("allows form redirects to the configured OIDC issuer origin", () => {
    expect(
      contentSecurityPolicyFor("https://identity.example.com/realms/sourcing"),
    ).toContain("form-action 'self' https://identity.example.com")
  })
})
