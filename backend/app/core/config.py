from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    environment: str = "development"
    database_url: str
    redis_url: str
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
    oidc_issuer: str
    oidc_audience: str
    apollo_api_key: SecretStr
    contact_encryption_key: SecretStr
    suppression_hmac_key: SecretStr
    webhook_hmac_key: SecretStr

    @classmethod
    def for_test(cls) -> "Settings":
        return cls(
            environment="test",
            database_url="postgresql+psycopg://postgres:postgres@localhost:5432/sourcing_test",
            redis_url="redis://localhost:6379/15",
            object_store_endpoint="http://localhost:9000",
            oidc_issuer="https://issuer.test/",
            oidc_audience="sourcing-api",
            apollo_api_key="test-apollo-key",
            contact_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            suppression_hmac_key="test-suppression-key",
            webhook_hmac_key="test-webhook-key",
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
