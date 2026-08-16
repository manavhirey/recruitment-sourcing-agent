import os

os.environ.update(
    {
        "DATABASE_URL": (
            "postgresql+psycopg://sourcing_api_test:sourcing-api-test"
            "@localhost:5432/sourcing_test"
        ),
        "MIGRATION_DATABASE_URL": (
            "postgresql+psycopg://postgres:postgres@localhost:5432/sourcing_test"
        ),
        "MAINTENANCE_DATABASE_URL": (
            "postgresql+psycopg://sourcing_maintenance:sourcing-maintenance-test"
            "@localhost:5432/sourcing_test"
        ),
        "REDIS_URL": "redis://localhost:6379/15",
        "OBJECT_STORE_ENDPOINT": "http://localhost:9000",
        "OBJECT_STORE_WRITER_ACCESS_KEY_ID": "test-writer-key",
        "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY": "test-writer-secret",
        "OBJECT_STORE_DELETE_ACCESS_KEY_ID": "test-delete-key",
        "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY": "test-delete-secret",
        "OBJECT_STORE_LIFECYCLE_ADMIN_ACCESS_KEY_ID": "test-lifecycle-key",
        "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY": "test-lifecycle-secret",
        "OIDC_ISSUER": "https://issuer.test/",
        "OIDC_AUDIENCE": "sourcing-api",
        "OPENAI_API_KEY": "test-openai-key",
        "APOLLO_API_KEY": "test-apollo-key",
        "CONTACT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "SUPPRESSION_HMAC_KEY": "test-suppression-key",
        "TELEMETRY_HMAC_KEY": "test-telemetry-key",
        "WEBHOOK_HMAC_KEY": "test-webhook-key",
    }
)
