# Application and migration rollback

Use immutable image digests and one release identifier across API, web, sourcing worker, maintenance worker, and scheduler.

1. Disable new sourcing/enrichment dispatch with release feature flags; keep review, privacy, and contact expiry available.
2. Stop the scheduler, then drain sourcing and maintenance queues. Do not terminate a task between an external provider submission and its persisted receipt/checkpoint.
3. Deploy only a previous API/worker image that reads `provider_connector_states`. A Task 13 worker does not; before using it, revoke the Apollo credential and enforce an external provider-egress deny, and keep both controls in place until a circuit-aware worker is restored.
4. Verify health dependencies, tenant isolation, privacy resumption, a read-only review path, and one deterministic fake-provider replay.
5. Only after old images are serving and queues are drained may a migration be downgraded. `0013_provider_connector_state` refuses downgrade while any provider is disabled. Do not override that safeguard; roll forward or retain the schema and external egress deny.
6. If a downgrade would remove data or a column consumed by queued tasks, roll forward instead. Restore from PITR only under the backup runbook and reconcile suppressions/deletion markers before traffic.

Rollback must not re-enable Apollo automatically. Preserve the durable connector-disabled state across application rollback. Record sanitized release digests, migration revisions, flags, queue counts, and verification outcomes; never record secrets or provider/candidate payloads.
