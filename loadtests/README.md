# Isolated capacity-test contract

`k6-sourcing.js` targets a disposable, non-production deployment. It deliberately has no provisioning or integrity endpoints in the product API. A separately deployed control harness must use a different HTTP origin and must reject production targets.

Required environment variables:

- `LOAD_TEST_BASE_URL`: the disposable product deployment under test.
- `LOAD_TEST_CONTROL_URL`: the separately authenticated, non-production control harness origin.
- `LOAD_TEST_CONTROL_TOKEN`: a short-lived harness credential. It is sent only to the control origin.

`POST /v1/load-tests` on the control harness accepts the requested target, 25 tenants, 10 users per tenant, zero pre-created runs, 300 deterministic fake-provider profiles per run, and returns an `isolated_load_test` state containing a unique `load_test_id`, the exact `target_base_url`, and 25 tenant/job records with 10 short-lived user tokens each. The harness must independently allowlist the target and fail closed if it is not disposable.

Exactly the first user VU for each tenant creates one run. The executable mapping contract proves these are 25 distinct tenants:

```sh
node loadtests/test-tenant-mapping.mjs
node --check loadtests/k6-sourcing.js
docker run --rm -v "$PWD/loadtests:/loadtests:ro" \
  grafana/k6@sha256:70af91f86cd8e142e0544a4edaf79835a80033f71974b92edd5ac36fd4442a7b \
  inspect /loadtests/k6-sourcing.js
```

After the workload, `GET /v1/load-tests/{load_test_id}/integrity` must derive its result from authoritative database and queue reads, not request counters. The k6 teardown requires exactly 7,500 unique run-candidate rows, no duplicate canonical provider identities, no cross-tenant response records, a completed deterministic provider, and drained queues within 600 seconds.

The script and contract test prove workload mechanics only. They do not supply the control harness, cloud capacity, alert destinations, or production-like evidence; the launch gate remains blocked until an approved isolated harness executes the scenario and retains its signed evidence.
