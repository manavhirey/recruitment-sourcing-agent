import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_RETENTION_DAYS = 30
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ObjectStoreClient(Protocol):
    def put_object(self, **kwargs: object) -> object: ...

    def delete_object(self, **kwargs: object) -> object: ...

    def head_object(self, **kwargs: object) -> object: ...

    def list_object_versions(self, **kwargs: object) -> object: ...

    def put_bucket_lifecycle_configuration(self, **kwargs: object) -> object: ...


@dataclass(frozen=True)
class SnapshotReceipt:
    reference: str
    checksum_sha256: str
    created_at: datetime
    expires_at: datetime


class SnapshotStore:
    def __init__(
        self,
        client: ObjectStoreClient,
        bucket: str,
        encryption_key: str | bytes,
    ) -> None:
        encoded = (
            encryption_key.encode()
            if isinstance(encryption_key, str)
            else encryption_key
        )
        try:
            self._key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("snapshot encryption key must be base64") from error
        if len(self._key) != 32:
            raise ValueError("snapshot encryption key must be 256-bit")
        self._client = client
        self._bucket = bucket

    def put(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        provider: str,
        request_id: str,
        payload: dict[str, object],
        created_at: datetime | None = None,
    ) -> SnapshotReceipt:
        reference = _reference(tenant_id, run_id, provider, request_id)
        timestamp = _utc(created_at or datetime.now(UTC))
        canonical = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
        data_key = AESGCM.generate_key(bit_length=256)
        nonce = secrets.token_bytes(12)
        key_nonce = secrets.token_bytes(12)
        aad = f"provider-snapshot-v1\0{reference}".encode()
        ciphertext = AESGCM(data_key).encrypt(nonce, canonical, aad)
        wrapped_key = AESGCM(self._key).encrypt(
            key_nonce, data_key, b"snapshot-data-key\0" + aad
        )
        body = (
            b"SNP1"
            + nonce
            + key_nonce
            + len(wrapped_key).to_bytes(2, "big")
            + wrapped_key
            + ciphertext
        )
        checksum = hashlib.sha256(body).hexdigest()
        expires_at = timestamp + timedelta(days=_RETENTION_DAYS)
        self._client.put_object(
            Bucket=self._bucket,
            Key=reference,
            Body=body,
            ContentType="application/octet-stream",
            Metadata={
                "checksum-sha256": checksum,
                "expires-at": expires_at.isoformat(),
            },
        )
        return SnapshotReceipt(reference, checksum, timestamp, expires_at)

    def delete(self, reference: str) -> None:
        purge_snapshot_versions(self._client, self._bucket, reference)

    def exists(self, reference: str) -> bool:
        validate_snapshot_reference(reference)
        try:
            self._client.head_object(Bucket=self._bucket, Key=reference)
        except (KeyError, FileNotFoundError):
            return False
        return True


def _reference(tenant_id: UUID, run_id: UUID, provider: str, request_id: str) -> str:
    for value in (provider, request_id):
        if not value or not _SAFE_PATH_COMPONENT.fullmatch(value):
            raise ValueError("snapshot path component is invalid")
    return f"{tenant_id}/{run_id}/{provider}/{request_id}"


def configure_snapshot_lifecycle(client: ObjectStoreClient, bucket: str) -> None:
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-provider-snapshots-30-days",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": _RETENTION_DAYS},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": _RETENTION_DAYS},
                }
            ]
        },
    )


def purge_snapshot_versions(
    client: ObjectStoreClient, bucket: str, reference: str
) -> None:
    """Permanently remove the exact snapshot key, including hidden versions."""
    validate_snapshot_reference(reference)
    versions: list[str] = []
    request: dict[str, object] = {"Bucket": bucket, "Prefix": reference}
    while True:
        response = client.list_object_versions(**request)
        if not isinstance(response, Mapping):
            raise TypeError("snapshot_version_listing_invalid")
        for group in ("Versions", "DeleteMarkers"):
            entries = response.get(group, [])
            if not isinstance(entries, list):
                raise TypeError("snapshot_version_listing_invalid")
            for entry in entries:
                if not isinstance(entry, Mapping) or entry.get("Key") != reference:
                    continue
                version_id = entry.get("VersionId")
                if not isinstance(version_id, str) or not version_id:
                    raise TypeError("snapshot_version_listing_invalid")
                versions.append(version_id)
        if response.get("IsTruncated") is not True:
            break
        next_key = response.get("NextKeyMarker")
        next_version = response.get("NextVersionIdMarker")
        if not isinstance(next_key, str) or not isinstance(next_version, str):
            raise TypeError("snapshot_version_listing_invalid")
        request["KeyMarker"] = next_key
        request["VersionIdMarker"] = next_version
    if not versions:
        client.delete_object(Bucket=bucket, Key=reference)
        return
    for version_id in versions:
        client.delete_object(Bucket=bucket, Key=reference, VersionId=version_id)


def validate_snapshot_reference(
    reference: str, *, tenant_id: UUID | None = None
) -> None:
    components = reference.split("/")
    if len(components) != 4:
        raise ValueError("snapshot reference is invalid")
    reference_tenant_id = UUID(components[0])
    UUID(components[1])
    if tenant_id is not None and reference_tenant_id != tenant_id:
        raise ValueError("snapshot reference tenant namespace is invalid")
    if not all(_SAFE_PATH_COMPONENT.fullmatch(value) for value in components[2:]):
        raise ValueError("snapshot reference is invalid")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
