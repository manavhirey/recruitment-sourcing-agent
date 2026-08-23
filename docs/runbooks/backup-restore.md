# Backup and isolated restore rehearsal

Owner: platform operations. Page on any failed scheduled rehearsal. Never restore into a production database name or network.

## Backup controls

- Enable PostgreSQL continuous WAL archiving and point-in-time recovery. Encrypt base backups and WAL with a managed key; retain them under the approved regional schedule.
- Enable object-store versioning and 30-day lifecycle expiry for encrypted provider snapshots. Backups store secret references, never secret values. Managed secret versions and KMS key recovery are verified separately.
- Database and object backups must have independent access identities. Runtime credentials cannot delete backup versions; maintenance deletion requires only `ListBucketVersions` plus `DeleteObjectVersion`/`DeleteObject` for the snapshot bucket and cannot read contact plaintext.
- Monitor backup age, WAL continuity, object lifecycle failures, and restore rehearsal results.

## Weekly isolated rehearsal

1. Create a private restore environment with no provider, email, or public egress. Generate a unique database name matching `sourcing_restore_<run identifier>`.
2. Set `RESTORE_SOURCE_DATABASE_URL`, `RESTORE_ADMIN_DATABASE_URL`, and `RESTORE_DATABASE_NAME` from managed secret references. Run `python backend/scripts/restore_rehearsal.py` from a PostgreSQL 16 client image.
3. The script creates a custom-format dump without ownership, restores without `--clean` or `--create`, compares Alembic revision and tenant/candidate/run-candidate/audit counts, exercises RLS as `sourcing_api`, and always drops the isolated database.
4. Restore object versions only into a separate bucket. The maintenance delete path enumerates and permanently removes every exact-key version and delete marker; lifecycle also expires noncurrent versions after 30 days. Never restore an expired snapshot or any object named by a completed privacy-deletion target. Reconcile restored versions against current suppression identifiers and privacy checkpoints, and prove no completed deletion key remains, before allowing application access.
5. Run `alembic upgrade head`, snapshot/contact expiry, privacy deletion resumption, and tenant-isolation E2E in the isolated environment. Do not call Apollo.
6. Record immutable evidence: backup timestamp, target recovery point, start/end time, schema revision, integrity counts, RLS result, object-version reconciliation result, and operator identity hash. Never attach dumps, JDs, provider payloads, contacts, or secrets.

Recovery objectives and retention are not asserted by this repository; operations and privacy reviewers must approve them for India and the US. A successful local dump/restore proves mechanics only, not cloud PITR or object recovery.

## Failure and escalation

- Any count mismatch, cross-tenant row, missing WAL segment, expired/deleted object resurrection, or cleanup failure is a page. Quarantine the restore environment and preserve sanitized command exit codes.
- The scheduled workflow failure is the repository-level signal; connect its GitHub environment to the approved page destination. Do not log connection URLs or command environments. Alert delivery remains a launch blocker until that destination is configured and tested.
- Production launch remains blocked until a cloud rehearsal has current evidence and the security/privacy reviewers approve it.
