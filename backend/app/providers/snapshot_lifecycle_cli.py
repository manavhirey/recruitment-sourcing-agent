from app.core.config import get_lifecycle_admin_settings
from app.providers.snapshots import configure_snapshot_lifecycle


def main() -> None:
    import boto3  # type: ignore[import-untyped]

    settings = get_lifecycle_admin_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.object_store_endpoint,
        aws_access_key_id=(
            settings.object_store_lifecycle_admin_access_key_id.get_secret_value()
        ),
        aws_secret_access_key=(
            settings.object_store_lifecycle_admin_secret_access_key.get_secret_value()
        ),
    )
    configure_snapshot_lifecycle(client, settings.object_store_bucket)


if __name__ == "__main__":
    main()
