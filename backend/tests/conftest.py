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
        "OBJECT_STORE_ADMIN_ACCESS_KEY_ID": "test-admin-key",
        "OBJECT_STORE_ADMIN_SECRET_ACCESS_KEY": "test-admin-secret",
        "OIDC_ISSUER": "https://issuer.test/",
        "OIDC_AUDIENCE": "sourcing-api",
        "OPENAI_API_KEY": "test-openai-key",
        "APOLLO_API_KEY": "test-apollo-key",
        "CONTACT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "SUPPRESSION_HMAC_KEY": "test-suppression-key",
        "WEBHOOK_HMAC_KEY": "test-webhook-key",
    }
)
