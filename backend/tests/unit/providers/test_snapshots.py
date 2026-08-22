import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.providers.snapshots import (
    SnapshotStore,
    configure_snapshot_lifecycle,
    purge_snapshot_versions,
    validate_snapshot_reference,
)


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict[str, object]] = []
        self.lifecycle: dict[str, object] | None = None
        self.version_entries: list[dict[str, str]] = []
        self.deleted_versions: list[str] = []

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(
            kwargs["Body"]  # type: ignore[arg-type]
        )

    def delete_object(self, **kwargs: object) -> None:
        if "VersionId" in kwargs:
            version_id = str(kwargs["VersionId"])
            self.deleted_versions.append(version_id)
            self.version_entries = [
                entry
                for entry in self.version_entries
                if entry["VersionId"] != version_id
            ]
            return
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)

    def list_object_versions(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Prefix"])
        return {
            "Versions": [
                entry for entry in self.version_entries if entry["Key"] == key
            ],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }

    def head_object(self, **kwargs: object) -> dict[str, object]:
        key = (str(kwargs["Bucket"]), str(kwargs["Key"]))
        if key not in self.objects:
            raise KeyError(key)
        return {}

    def put_bucket_lifecycle_configuration(self, **kwargs: object) -> None:
        self.lifecycle = kwargs


def _store(fake: FakeObjectStore) -> SnapshotStore:
    return SnapshotStore(
        fake,
        "snapshots",
        base64.b64encode(b"s" * 32).decode(),
    )


def test_snapshot_is_encrypted_checksummed_and_expires_at_exactly_30_days() -> None:
    fake = FakeObjectStore()
    store = _store(fake)
    tenant_id, run_id = uuid4(), uuid4()
    created_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    payload = {"email": "priya@example.com", "request_id": "123"}

    receipt = store.put(
        tenant_id=tenant_id,
        run_id=run_id,
        provider="apollo",
        request_id="123",
        payload=payload,
        created_at=created_at,
    )

    body = fake.put_calls[0]["Body"]
    assert isinstance(body, bytes)
    assert b"priya@example.com" not in body
    assert receipt.reference == f"{tenant_id}/{run_id}/apollo/123"
    assert receipt.checksum_sha256 == hashlib.sha256(body).hexdigest()
    assert receipt.expires_at == created_at + timedelta(days=30)
    assert fake.put_calls[0]["ContentType"] == "application/octet-stream"


def test_snapshot_path_components_are_validated_and_delete_is_idempotent() -> None:
    fake = FakeObjectStore()
    store = _store(fake)
    receipt = store.put(
        tenant_id=uuid4(),
        run_id=uuid4(),
        provider="apollo",
        request_id="request-1",
        payload={"people": []},
    )

    store.delete(receipt.reference)
    store.delete(receipt.reference)

    assert not fake.objects


def test_snapshot_delete_purges_every_version_so_privacy_data_cannot_return() -> None:
    fake = FakeObjectStore()
    store = _store(fake)
    reference = f"{uuid4()}/{uuid4()}/apollo/request-versioned"
    fake.version_entries = [
        {"Key": reference, "VersionId": "version-1"},
        {"Key": reference, "VersionId": "version-2"},
    ]

    store.delete(reference)

    assert fake.version_entries == []
    assert fake.deleted_versions == ["version-1", "version-2"]


def test_version_purge_paginates_and_ignores_prefix_collisions() -> None:
    fake = FakeObjectStore()
    reference = f"{uuid4()}/{uuid4()}/apollo/request-versioned"
    requests: list[dict[str, object]] = []

    def list_versions(**kwargs: object) -> dict[str, object]:
        requests.append(kwargs)
        if len(requests) == 1:
            return {
                "Versions": [
                    {"Key": reference, "VersionId": "v1"},
                    {"Key": f"{reference}-other", "VersionId": "foreign"},
                ],
                "IsTruncated": True,
                "NextKeyMarker": reference,
                "NextVersionIdMarker": "v1",
            }
        return {
            "DeleteMarkers": [{"Key": reference, "VersionId": "marker"}],
            "IsTruncated": False,
        }

    fake.list_object_versions = list_versions  # type: ignore[method-assign]

    purge_snapshot_versions(fake, "snapshots", reference)

    assert requests[1]["KeyMarker"] == reference
    assert requests[1]["VersionIdMarker"] == "v1"
    assert fake.deleted_versions == ["v1", "marker"]


def test_snapshot_exists_checks_the_exact_encrypted_object() -> None:
    fake = FakeObjectStore()
    store = _store(fake)
    reference = f"{uuid4()}/{uuid4()}/apollo/request"

    assert store.exists(reference) is False
    fake.objects[("snapshots", reference)] = b"encrypted"
    assert store.exists(reference) is True


def test_snapshot_lifecycle_uses_exactly_30_days() -> None:
    fake = FakeObjectStore()

    configure_snapshot_lifecycle(fake, "snapshots")

    assert fake.lifecycle == {
        "Bucket": "snapshots",
        "LifecycleConfiguration": {
            "Rules": [
                {
                    "ID": "expire-provider-snapshots-30-days",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": 30},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                }
            ]
        },
    }


def test_snapshot_reference_can_be_bound_to_expected_tenant_namespace() -> None:
    expected_tenant = uuid4()
    other_tenant = uuid4()
    reference = f"{other_tenant}/{uuid4()}/apollo/request-1"

    with pytest.raises(ValueError, match="tenant namespace"):
        validate_snapshot_reference(reference, tenant_id=expected_tenant)
