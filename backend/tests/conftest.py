import os

os.environ.update(
    {
        "DATABASE_URL": "postgresql+psycopg://postgres:postgres@localhost:5432/sourcing_test",
        "REDIS_URL": "redis://localhost:6379/15",
        "OBJECT_STORE_ENDPOINT": "http://localhost:9000",
        "OIDC_ISSUER": "https://issuer.test/",
        "OIDC_AUDIENCE": "sourcing-api",
        "APOLLO_API_KEY": "test-apollo-key",
        "CONTACT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "SUPPRESSION_HMAC_KEY": "test-suppression-key",
        "WEBHOOK_HMAC_KEY": "test-webhook-key",
    }
)
