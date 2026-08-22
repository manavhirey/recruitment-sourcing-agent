import exec from "k6/execution"
import http from "k6/http"
import { check, fail, sleep } from "k6"
import { Counter, Rate, Trend } from "k6/metrics"

import { assignmentForVu, shouldLaunchTenantRun } from "./tenant-mapping.mjs"

const readLatency = new Trend("foreground_read_latency", true)
const mutationLatency = new Trend("mutation_latency", true)
const requestErrors = new Rate("request_errors")
const integrityFailures = new Counter("integrity_failures")
const crossTenantRecords = new Counter("cross_tenant_records")

const baseUrl = __ENV.LOAD_TEST_BASE_URL
const controlUrl = __ENV.LOAD_TEST_CONTROL_URL
const controlToken = __ENV.LOAD_TEST_CONTROL_TOKEN

function origin(url) {
  const match = /^https?:\/\/[^/]+/.exec(url)
  return match ? match[0].toLowerCase() : ""
}

export const options = {
  scenarios: {
    recruiters: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "2m", target: 250 },
        { duration: "10m", target: 250 },
        { duration: "2m", target: 0 },
      ],
      gracefulRampDown: "1m",
    },
  },
  thresholds: {
    "foreground_read_latency": ["p(95)<500", "p(99)<1000"],
    "mutation_latency": ["p(95)<750"],
    "request_errors": ["rate<0.01"],
    "integrity_failures": ["count==0"],
    "cross_tenant_records": ["count==0"],
  },
}

function headers(token, tenantId) {
  return {
    Authorization: `Bearer ${token}`,
    "X-Tenant-ID": tenantId,
    "Content-Type": "application/json",
  }
}

function observe(response, metric) {
  metric.add(response.timings.duration)
  requestErrors.add(response.status >= 400)
  return response
}

export function setup() {
  if (!baseUrl || !controlUrl || !controlToken) {
    fail("LOAD_TEST_BASE_URL, LOAD_TEST_CONTROL_URL, and LOAD_TEST_CONTROL_TOKEN are required")
  }
  if (!origin(baseUrl) || !origin(controlUrl) || origin(baseUrl) === origin(controlUrl)) {
    fail("load-test target and isolated control harness must use separate HTTP origins")
  }
  const response = http.post(
    `${controlUrl}/v1/load-tests`,
    JSON.stringify({
      target_base_url: baseUrl,
      tenant_count: 25,
      users_per_tenant: 10,
      runs_per_tenant: 0,
      profiles_per_run: 300,
      provider: "deterministic_fake",
    }),
    { headers: { Authorization: `Bearer ${controlToken}`, "Content-Type": "application/json" } },
  )
  if (response.status !== 201) fail(`load harness provisioning failed: ${response.status}`)
  const state = response.json()
  if (
    state.environment !== "isolated_load_test" ||
    state.provider !== "deterministic_fake" ||
    typeof state.load_test_id !== "string" ||
    state.target_base_url !== baseUrl ||
    !Array.isArray(state.tenants) ||
    state.tenants.length !== 25 ||
    state.tenants.some((tenant) => !Array.isArray(tenant.users) || tenant.users.length !== 10)
  ) {
    fail("load harness refused safe deterministic contract")
  }
  return state
}

export default function (state) {
  const { tenantNumber, userNumber } = assignmentForVu(exec.vu.idInTest)
  const tenant = state.tenants[tenantNumber]
  const user = tenant.users[userNumber]
  const requestHeaders = headers(user.token, tenant.id)

  if (shouldLaunchTenantRun(exec.vu.idInTest, exec.vu.iterationInInstance)) {
    const runResponse = observe(
      http.post(
        `${baseUrl}/api/v1/jobs/${tenant.job_id}/runs`,
        JSON.stringify({}),
        { headers: { ...requestHeaders, "Idempotency-Key": `load-run-${tenant.id}` } },
      ),
      mutationLatency,
    )
    check(runResponse, { "run accepted": (response) => response.status === 201 || response.status === 200 })
  }

  const statusResponse = observe(
    http.get(`${baseUrl}/api/v1/jobs/${tenant.job_id}/runs/latest`, { headers: requestHeaders }),
    readLatency,
  )
  if (statusResponse.status === 200) {
    const run = statusResponse.json()
    const page = observe(
      http.get(`${baseUrl}/api/v1/jobs/${tenant.job_id}/candidates?classification=main&sort=-score`, { headers: requestHeaders }),
      readLatency,
    )
    if (page.status === 200 && page.json().items.length > 0) {
      const candidate = page.json().items[0]
      observe(
        http.get(`${baseUrl}/api/v1/job-candidates/${candidate.id}`, { headers: requestHeaders }),
        readLatency,
      )
      observe(
        http.patch(
          `${baseUrl}/api/v1/job-candidates/${candidate.id}/stage`,
          JSON.stringify({ stage: "Shortlisted" }),
          { headers: { ...requestHeaders, "Idempotency-Key": `load-shortlist-${candidate.id}` } },
        ),
        mutationLatency,
      )
      observe(
        http.get(`${baseUrl}/api/v1/jobs/${tenant.job_id}/export.csv`, { headers: requestHeaders }),
        readLatency,
      )
    }
    if (["ready", "partially_ready", "failed", "cancelled"].includes(run.state)) sleep(1)
  }

  const otherTenant = state.tenants[(tenantNumber + 1) % state.tenants.length]
  const isolation = http.get(`${baseUrl}/api/v1/jobs/${otherTenant.job_id}`, { headers: requestHeaders })
  if (isolation.status !== 404) crossTenantRecords.add(1)
  sleep(1)
}

export function teardown(state) {
  const response = http.get(`${controlUrl}/v1/load-tests/${encodeURIComponent(state.load_test_id)}/integrity`, {
    headers: { Authorization: `Bearer ${controlToken}` },
    timeout: "10m",
  })
  if (response.status !== 200) {
    integrityFailures.add(1)
    return
  }
  const result = response.json()
  const valid =
    result.run_candidate_count === 7500 &&
    result.unique_run_candidate_count === 7500 &&
    result.duplicate_canonical_provider_ids === 0 &&
    result.cross_tenant_response_records === 0 &&
    result.queues_drained === true &&
    result.provider_completed_at !== null &&
    result.queues_drained_within_seconds <= 600
  if (!valid) integrityFailures.add(1)
  check(result, { "authoritative integrity proof passed": () => valid })
}
