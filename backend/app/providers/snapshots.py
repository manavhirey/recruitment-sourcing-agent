import base64
import binascii
import hashlib
import json
import re
import secrets
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
        validate_snapshot_reference(reference)
        self._client.delete_object(Bucket=self._bucket, Key=reference)

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
                }
            ]
        },
    )


def validate_snapshot_reference(reference: str) -> None:
    components = reference.split("/")
    if len(components) != 4:
        raise ValueError("snapshot reference is invalid")
    UUID(components[0])
    UUID(components[1])
    if not all(_SAFE_PATH_COMPONENT.fullmatch(value) for value in components[2:]):
        raise ValueError("snapshot reference is invalid")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
