"""Export the backend application's OpenAPI document deterministically."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


SAFE_GENERATION_ENV = {
    "DATABASE_URL": "postgresql+psycopg://api:api@localhost:5432/openapi",
    "REDIS_URL": "redis://localhost:6379/15",
    "OBJECT_STORE_ENDPOINT": "http://localhost:9000",
    "OBJECT_STORE_WRITER_ACCESS_KEY_ID": "openapi-writer",
    "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY": "openapi-writer-secret",
    "OBJECT_STORE_DELETE_ACCESS_KEY_ID": "openapi-delete",
    "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY": "openapi-delete-secret",
    "OBJECT_STORE_LIFECYCLE_ADMIN_ACCESS_KEY_ID": "openapi-lifecycle",
    "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY": "openapi-lifecycle-secret",
    "OIDC_ISSUER": "https://issuer.openapi.invalid/",
    "OIDC_AUDIENCE": "sourcing-api",
    "OPENAI_API_KEY": "openapi-generation-key",
    "APOLLO_API_KEY": "openapi-generation-key",
    "CONTACT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SUPPRESSION_HMAC_KEY": "openapi-generation-suppression-key",
    "WEBHOOK_HMAC_KEY": "openapi-generation-webhook-key",
}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: export-openapi.py OUTPUT")
    for name, value in SAFE_GENERATION_ENV.items():
        os.environ.setdefault(name, value)
    repository_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repository_root / "backend"))

    from app.core.config import Settings  # noqa: PLC0415
    from app.main import create_app  # noqa: PLC0415

    document = create_app(Settings()).openapi()
    output = Path(sys.argv[1])
    output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
