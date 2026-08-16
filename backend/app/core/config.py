from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: str = "development"
    database_url: str
    maintenance_database_url: str
    redis_url: str
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
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
    def require_distinct_database_roles(self) -> "Settings":
        api_role = make_url(self.database_url).username
        if api_role is None:
            raise ValueError("DATABASE_URL must include a database role")
        maintenance_role = make_url(self.maintenance_database_url).username
        if maintenance_role is None or maintenance_role == api_role:
            raise ValueError(
                "MAINTENANCE_DATABASE_URL must use a role distinct from API"
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
            maintenance_database_url=(
                "postgresql+psycopg://sourcing_maintenance:sourcing-maintenance-test"
                "@localhost:5432/sourcing_test"
            ),
            redis_url="redis://localhost:6379/15",
            object_store_endpoint="http://localhost:9000",
            oidc_issuer="https://issuer.test/",
            oidc_audience="sourcing-api",
            openai_api_key="test-openai-key",
            apollo_api_key="test-apollo-key",
            contact_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            suppression_hmac_key="test-suppression-key",
            webhook_hmac_key="test-webhook-key",
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
def get_migration_settings() -> MigrationSettings:
    return MigrationSettings()
