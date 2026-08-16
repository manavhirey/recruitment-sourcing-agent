# Apollo provider outage

Apollo is the only enabled provider. Never fall back to scraping, a recruiter browser session, or an unapproved connector.

## Detection thresholds

- Any `401` or `403` disables the shared Apollo connector in `provider_connector_states` and pages platform operations immediately.
- Page when provider error outcomes exceed 20% for 10 minutes, a queue exceeds 1,000 messages for 10 minutes, any run remains beyond its stage threshold for 10 minutes, or more than five webhook failures occur in five minutes.
- A fixed-window `429` pauses only the affected task until the provider reset time. Bounded `5xx` and timeouts retry with jitter. Malformed `200` responses are quarantined and never persisted as candidate facts.
- Tenant credit exhaustion creates only Owner/Admin notifications for that tenant and moves its run to Partially Ready. It must not disable Apollo for other tenants.

## Response matrix

| Signal | Automated state | Operator action |
| --- | --- | --- |
| 401 | Shared connector disabled; run safely fails/partial | Rotate/verify managed credential and contract, then smoke test |
| 403 | Shared connector disabled; run safely fails/partial | Confirm endpoint permission and contract |
| 429 | Task rescheduled at reset; reservation retained | Watch queue and provider window |
| 5xx/timeout | Bounded exponential retry | Escalate if 20%/10m threshold persists |
| Malformed payload | Payload rejected; no canonical write | Compare redacted schema metadata with contract |
| Lost webhook | Polling path applies the same idempotent handler | Verify polling age and callback capability health |
| Quota exhausted | One tenant Partially Ready and notified | Owner/Admin raises or waits for tenant budget |

Do not paste provider bodies, callback URLs, tokens, contacts, snapshots, or JDs into alerts or tickets. Use run ID, endpoint allowlist, sanitized error code, retry count, and provider request identifier only where contractually permitted.

## Recovery

1. Confirm the platform-owned credential and Apollo contract in the approved staging account.
2. Run one low-budget search and enrichment against de-identified test data. Confirm request accounting, callback/poll recovery, and no sensitive telemetry.
3. An authorized platform operator resets `provider_connector_states.enabled=true`, clears the allowlisted reason, and records a separate operational audit/change ticket. Tenant users cannot reset the circuit.
4. Resume/replay durable checkpoints. Do not create a new provider request when an enrichment submission outcome is ambiguous.
5. Verify queue drain, stuck-run alerts, partial results, and tenant-local budget behavior.

Launch is blocked until contracted staging credentials, alert destinations, and provider-contract approval exist.
