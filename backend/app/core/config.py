from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class _ObjectStoreAccessKeyIdentities(BaseSettings):
    """Load only access-key identities so capability credentials can be compared."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    object_store_writer_access_key_id: SecretStr
    object_store_delete_access_key_id: SecretStr
    object_store_lifecycle_admin_access_key_id: SecretStr


def _require_distinct_object_store_identity(
    identity: SecretStr,
    *,
    other_capabilities: tuple[str, str],
) -> None:
    identities = _ObjectStoreAccessKeyIdentities()
    supplied = identity.get_secret_value()
    other_values = {
        getattr(identities, field).get_secret_value() for field in other_capabilities
    }
    if supplied in other_values:
        raise ValueError("object-store access-key identities must be distinct")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: str = "development"
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
    apollo_api_key: SecretStr
    contact_encryption_key: SecretStr
    suppression_hmac_key: SecretStr
    webhook_hmac_key: SecretStr
    webhook_base_url: str = "https://localhost"
    webhook_max_body_bytes: int = 262_144
    webhook_trusted_proxy_ips: str = ""
    apollo_reveal_personal_emails: bool = False
    apollo_reveal_phone_numbers: bool = False

    @model_validator(mode="after")
    def require_dedicated_writer_identity(self) -> "Settings":
        _require_distinct_object_store_identity(
            self.object_store_writer_access_key_id,
            other_capabilities=(
                "object_store_delete_access_key_id",
                "object_store_lifecycle_admin_access_key_id",
            ),
        )
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
            apollo_api_key="test-apollo-key",
            contact_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            suppression_hmac_key="test-suppression-key",
            webhook_hmac_key="test-webhook-key",
        )


class MaintenanceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    maintenance_database_url: str
    redis_url: str
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
    object_store_delete_access_key_id: SecretStr
    object_store_delete_secret_access_key: SecretStr

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
        return self

    @classmethod
    def for_test(cls) -> "MaintenanceSettings":
        return cls(
            maintenance_database_url=(
                "postgresql+psycopg://sourcing_maintenance:sourcing-maintenance-test"
                "@localhost:5432/sourcing_test"
            ),
            redis_url="redis://localhost:6379/15",
            object_store_endpoint="http://localhost:9000",
            object_store_delete_access_key_id="test-delete-key",
            object_store_delete_secret_access_key="test-delete-secret",
        )


class LifecycleAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

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
        return self

    @classmethod
    def for_test(cls) -> "LifecycleAdminSettings":
        return cls(
            object_store_endpoint="http://localhost:9000",
            object_store_lifecycle_admin_access_key_id="test-lifecycle-key",
            object_store_lifecycle_admin_secret_access_key="test-lifecycle-secret",
        )


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_maintenance_settings() -> MaintenanceSettings:
    return MaintenanceSettings()


@lru_cache
def get_lifecycle_admin_settings() -> LifecycleAdminSettings:
    return LifecycleAdminSettings()


@lru_cache
def get_migration_settings() -> MigrationSettings:
    return MigrationSettings()
