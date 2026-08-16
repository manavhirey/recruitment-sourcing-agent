import base64
import binascii
import hashlib
import hmac
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "development",
    "example",
    "placeholder",
    "replace",
    "sample",
    "test-",
)
Environment = Literal["development", "test", "production"]


def _secret_value(value: SecretStr) -> str:
    return value.get_secret_value()


def _require_production_secrets(
    environment: str,
    secrets: dict[str, tuple[SecretStr, int]],
) -> None:
    if environment.casefold() != "production":
        return
    supplied: dict[str, str] = {}
    for name, (secret, minimum_length) in secrets.items():
        value = _secret_value(secret)
        normalized = value.casefold()
        if len(value) < minimum_length or any(
            marker in normalized for marker in _PLACEHOLDER_MARKERS
        ):
            raise ValueError(f"{name} is not production-safe")
        supplied[name] = value
    if len(set(supplied.values())) != len(supplied):
        raise ValueError("production secret capabilities must be distinct")


def _require_secure_url(name: str, value: str, *, schemes: set[str]) -> None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() not in schemes or not host:
        raise ValueError(f"{name} must use a secure URL")
    if host in {"localhost", "127.0.0.1", "::1"} or any(
        marker in host for marker in ("example", "placeholder", ".invalid")
    ):
        raise ValueError(f"{name} must not use a placeholder host")


def _require_production_database_url(name: str, value: str) -> None:
    parsed = make_url(value)
    host = (parsed.host or "").casefold()
    if parsed.get_backend_name() != "postgresql" or not host:
        raise ValueError(f"{name} must use PostgreSQL")
    if host in {"localhost", "127.0.0.1", "::1"} or any(
        marker in host for marker in ("example", "placeholder", ".invalid")
    ):
        raise ValueError(f"{name} must not use a placeholder host")
    if parsed.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise ValueError(f"{name} must require PostgreSQL TLS")
    password = parsed.password or ""
    if len(password) < 24 or any(
        marker in password.casefold() for marker in _PLACEHOLDER_MARKERS
    ):
        raise ValueError(f"{name} credentials are not production-safe")


def _require_contact_encryption_key(value: SecretStr) -> None:
    try:
        decoded_key = base64.b64decode(value.get_secret_value(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("contact_encryption_key is invalid") from error
    if len(decoded_key) != 32:
        raise ValueError("contact_encryption_key is invalid")


def _require_production_redis_url(value: str) -> None:
    _require_secure_url("redis_url", value, schemes={"rediss"})
    password = urlsplit(value).password or ""
    if len(password) < 24 or any(
        marker in password.casefold() for marker in _PLACEHOLDER_MARKERS
    ):
        raise ValueError("redis_url credentials are not production-safe")


def _require_key_version(name: str, value: str) -> None:
    if not re.fullmatch(r"v[1-9][0-9]{0,5}", value):
        raise ValueError(f"{name} is invalid")


def derive_identity_hmac_key(settings: "Settings | WorkerSettings") -> bytes:
    return hmac.digest(
        settings.identity_hmac_key.get_secret_value().encode(),
        b"invitation-idempotency\0" + settings.identity_hmac_key_version.encode(),
        hashlib.sha256,
    )


class _ObjectStoreAccessKeyIdentities(BaseSettings):
    """Load only access-key identities so capability credentials can be compared."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    object_store_writer_access_key_id: SecretStr | None = None
    object_store_delete_access_key_id: SecretStr | None = None
    object_store_lifecycle_admin_access_key_id: SecretStr | None = None


def _require_distinct_object_store_identity(
    identity: SecretStr,
    *,
    other_capabilities: tuple[str, str],
) -> None:
    identities = _ObjectStoreAccessKeyIdentities()
    supplied = identity.get_secret_value()
    other_values = {
        value.get_secret_value()
        for field in other_capabilities
        if (value := getattr(identities, field)) is not None
    }
    if supplied in other_values:
        raise ValueError("object-store access-key identities must be distinct")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: Environment = "development"
    database_url: str
    redis_url: str
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
    object_store_writer_access_key_id: SecretStr
    object_store_writer_secret_access_key: SecretStr
    oidc_issuer: str
    oidc_audience: str
    openai_api_key: SecretStr
    scorecard_model: str = "gpt-5-mini"
    contact_encryption_key: SecretStr
    suppression_hmac_key: SecretStr
    identity_hmac_key: SecretStr = SecretStr("development-identity-key")
    telemetry_hmac_key: SecretStr
    suppression_hmac_key_version: str = "v1"
    identity_hmac_key_version: str = "v1"
    webhook_hmac_key: SecretStr
    webhook_base_url: str = "https://localhost"
    webhook_max_body_bytes: int = 262_144
    webhook_trusted_proxy_ips: str = ""

    @model_validator(mode="after")
    def require_dedicated_writer_identity(self) -> "Settings":
        _require_distinct_object_store_identity(
            self.object_store_writer_access_key_id,
            other_capabilities=(
                "object_store_delete_access_key_id",
                "object_store_lifecycle_admin_access_key_id",
            ),
        )
        _require_key_version(
            "identity_hmac_key_version", self.identity_hmac_key_version
        )
        if self.environment == "production":
            _require_production_database_url("database_url", self.database_url)
            _require_production_redis_url(self.redis_url)
            _require_secure_url(
                "object_store_endpoint",
                self.object_store_endpoint,
                schemes={"https"},
            )
            _require_secure_url("oidc_issuer", self.oidc_issuer, schemes={"https"})
            _require_secure_url(
                "webhook_base_url", self.webhook_base_url, schemes={"https"}
            )
            _require_production_secrets(
                self.environment,
                {
                    "object_store_writer_access_key_id": (
                        self.object_store_writer_access_key_id,
                        16,
                    ),
                    "object_store_writer_secret_access_key": (
                        self.object_store_writer_secret_access_key,
                        32,
                    ),
                    "openai_api_key": (self.openai_api_key, 24),
                    "contact_encryption_key": (self.contact_encryption_key, 43),
                    "suppression_hmac_key": (self.suppression_hmac_key, 32),
                    "identity_hmac_key": (self.identity_hmac_key, 32),
                    "telemetry_hmac_key": (self.telemetry_hmac_key, 32),
                    "webhook_hmac_key": (self.webhook_hmac_key, 32),
                },
            )
            _require_contact_encryption_key(self.contact_encryption_key)
        return self

    @classmethod
    def for_test(cls) -> "Settings":
        return cls(
            environment="test",
            database_url=(
                "postgresql+psycopg://sourcing_api_test:sourcing-api-test"
                "@localhost:5432/sourcing_test"
            ),
            redis_url="redis://localhost:6379/15",
            object_store_endpoint="http://localhost:9000",
            object_store_writer_access_key_id="test-writer-key",
            object_store_writer_secret_access_key="test-writer-secret",
            oidc_issuer="https://issuer.test/",
            oidc_audience="sourcing-api",
            openai_api_key="test-openai-key",
            contact_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            suppression_hmac_key="test-suppression-key",
            identity_hmac_key="test-identity-key",
            telemetry_hmac_key="test-telemetry-key",
            webhook_hmac_key="test-webhook-key",
        )


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: Environment = "development"
    database_url: str
    redis_url: str
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
    object_store_writer_access_key_id: SecretStr
    object_store_writer_secret_access_key: SecretStr
    apollo_api_key: SecretStr
    contact_encryption_key: SecretStr
    suppression_hmac_key: SecretStr
    identity_hmac_key: SecretStr = SecretStr("development-identity-key")
    telemetry_hmac_key: SecretStr
    suppression_hmac_key_version: str = "v1"
    identity_hmac_key_version: str = "v1"
    webhook_hmac_key: SecretStr
    webhook_base_url: str = "https://localhost"
    apollo_reveal_personal_emails: bool = False
    apollo_reveal_phone_numbers: bool = False
    apollo_contact_retention_days: int = Field(default=180, ge=1, le=180)

    @model_validator(mode="after")
    def require_dedicated_writer_identity(self) -> "WorkerSettings":
        _require_distinct_object_store_identity(
            self.object_store_writer_access_key_id,
            other_capabilities=(
                "object_store_delete_access_key_id",
                "object_store_lifecycle_admin_access_key_id",
            ),
        )
        _require_key_version(
            "identity_hmac_key_version", self.identity_hmac_key_version
        )
        if self.environment == "production":
            _require_production_database_url("database_url", self.database_url)
            _require_production_redis_url(self.redis_url)
            _require_secure_url(
                "object_store_endpoint",
                self.object_store_endpoint,
                schemes={"https"},
            )
            _require_secure_url(
                "webhook_base_url", self.webhook_base_url, schemes={"https"}
            )
            _require_production_secrets(
                self.environment,
                {
                    "object_store_writer_access_key_id": (
                        self.object_store_writer_access_key_id,
                        16,
                    ),
                    "object_store_writer_secret_access_key": (
                        self.object_store_writer_secret_access_key,
                        32,
                    ),
                    "apollo_api_key": (self.apollo_api_key, 24),
                    "contact_encryption_key": (self.contact_encryption_key, 43),
                    "suppression_hmac_key": (self.suppression_hmac_key, 32),
                    "identity_hmac_key": (self.identity_hmac_key, 32),
                    "telemetry_hmac_key": (self.telemetry_hmac_key, 32),
                    "webhook_hmac_key": (self.webhook_hmac_key, 32),
                },
            )
            _require_contact_encryption_key(self.contact_encryption_key)
        return self

    @classmethod
    def for_test(cls) -> "WorkerSettings":
        values = Settings.for_test().model_dump()
        return cls(**values, apollo_api_key="test-apollo-key")


class EnrichmentPolicySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    suppression_hmac_key: SecretStr
    suppression_hmac_key_version: str = "v1"
    apollo_contact_retention_days: int = Field(default=180, ge=1, le=180)


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: Environment = "development"
    redis_url: str

    @model_validator(mode="after")
    def require_secure_production_broker(self) -> "SchedulerSettings":
        if self.environment == "production":
            _require_production_redis_url(self.redis_url)
        return self


class MaintenanceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: Environment = "development"
    maintenance_database_url: str
    redis_url: str
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
    object_store_delete_access_key_id: SecretStr
    object_store_delete_secret_access_key: SecretStr
    telemetry_hmac_key: SecretStr

    @model_validator(mode="after")
    def require_dedicated_maintenance_role(self) -> "MaintenanceSettings":
        role = make_url(self.maintenance_database_url).username
        if role != "sourcing_maintenance":
            raise ValueError(
                "MAINTENANCE_DATABASE_URL must use the dedicated maintenance role"
            )
        _require_distinct_object_store_identity(
            self.object_store_delete_access_key_id,
            other_capabilities=(
                "object_store_writer_access_key_id",
                "object_store_lifecycle_admin_access_key_id",
            ),
        )
        if self.environment == "production":
            _require_production_database_url(
                "maintenance_database_url", self.maintenance_database_url
            )
            _require_production_redis_url(self.redis_url)
            _require_secure_url(
                "object_store_endpoint",
                self.object_store_endpoint,
                schemes={"https"},
            )
            _require_production_secrets(
                self.environment,
                {
                    "object_store_delete_access_key_id": (
                        self.object_store_delete_access_key_id,
                        16,
                    ),
                    "object_store_delete_secret_access_key": (
                        self.object_store_delete_secret_access_key,
                        32,
                    ),
                    "telemetry_hmac_key": (self.telemetry_hmac_key, 32),
                },
            )
        return self

    @classmethod
    def for_test(cls) -> "MaintenanceSettings":
        return cls(
            environment="test",
            maintenance_database_url=(
                "postgresql+psycopg://sourcing_maintenance:sourcing-maintenance-test"
                "@localhost:5432/sourcing_test"
            ),
            redis_url="redis://localhost:6379/15",
            object_store_endpoint="http://localhost:9000",
            object_store_delete_access_key_id="test-delete-key",
            object_store_delete_secret_access_key="test-delete-secret",
            telemetry_hmac_key="test-telemetry-key",
        )


class LifecycleAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: Environment = "development"
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
    object_store_lifecycle_admin_access_key_id: SecretStr
    object_store_lifecycle_admin_secret_access_key: SecretStr

    @model_validator(mode="after")
    def require_dedicated_lifecycle_admin_identity(self) -> "LifecycleAdminSettings":
        _require_distinct_object_store_identity(
            self.object_store_lifecycle_admin_access_key_id,
            other_capabilities=(
                "object_store_writer_access_key_id",
                "object_store_delete_access_key_id",
            ),
        )
        if self.environment == "production":
            _require_secure_url(
                "object_store_endpoint",
                self.object_store_endpoint,
                schemes={"https"},
            )
            _require_production_secrets(
                self.environment,
                {
                    "object_store_lifecycle_admin_access_key_id": (
                        self.object_store_lifecycle_admin_access_key_id,
                        16,
                    ),
                    "object_store_lifecycle_admin_secret_access_key": (
                        self.object_store_lifecycle_admin_secret_access_key,
                        32,
                    ),
                },
            )
        return self

    @classmethod
    def for_test(cls) -> "LifecycleAdminSettings":
        return cls(
            environment="test",
            object_store_endpoint="http://localhost:9000",
            object_store_lifecycle_admin_access_key_id="test-lifecycle-key",
            object_store_lifecycle_admin_secret_access_key="test-lifecycle-secret",
        )


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: Environment = "development"
    database_url: str
    migration_database_url: str
    maintenance_database_url: str

    @model_validator(mode="after")
    def require_distinct_database_roles(self) -> "MigrationSettings":
        roles = {
            make_url(value).username
            for value in (
                self.database_url,
                self.migration_database_url,
                self.maintenance_database_url,
            )
        }
        if None in roles or len(roles) != 3:
            raise ValueError(
                "API, migration, and maintenance database roles must be distinct"
            )
        if self.environment == "production":
            database_urls = {
                "database_url": self.database_url,
                "migration_database_url": self.migration_database_url,
                "maintenance_database_url": self.maintenance_database_url,
            }
            for name, value in database_urls.items():
                _require_production_database_url(name, value)
            passwords = {make_url(value).password for value in database_urls.values()}
            if None in passwords or len(passwords) != len(database_urls):
                raise ValueError("production database credentials must be distinct")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()


@lru_cache
def get_enrichment_policy_settings() -> EnrichmentPolicySettings:
    return EnrichmentPolicySettings()


@lru_cache
def get_scheduler_settings() -> SchedulerSettings:
    return SchedulerSettings()


@lru_cache
def get_maintenance_settings() -> MaintenanceSettings:
    return MaintenanceSettings()


@lru_cache
def get_lifecycle_admin_settings() -> LifecycleAdminSettings:
    return LifecycleAdminSettings()


@lru_cache
def get_migration_settings() -> MigrationSettings:
    return MigrationSettings()
