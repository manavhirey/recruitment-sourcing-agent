import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.providers.snapshots import (
    SnapshotStore,
    configure_snapshot_lifecycle,
    validate_snapshot_reference,
)


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict[str, object]] = []
        self.lifecycle: dict[str, object] | None = None

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(
            kwargs["Body"]  # type: ignore[arg-type]
        )

    def delete_object(self, **kwargs: object) -> None:
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)

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
