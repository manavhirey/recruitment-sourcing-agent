from uuid import UUID

from app.privacy.service import SuppressionService

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")


def test_suppression_digest_is_normalized_tenant_scoped_and_domain_separated() -> None:
    service = SuppressionService(None, b"suppression-secret", key_version="2026-01")

    first = service.digest(TENANT_A, "email", " Priya@Example.com ")
    same = service.digest(TENANT_A, "email", "priya@example.com")
    same_type_domain = service.digest(TENANT_A, " Email ", "priya@example.com")
    other_tenant = service.digest(TENANT_B, "email", "priya@example.com")
    other_kind = service.digest(
        TENANT_A,
        "provider_id:apollo",
        "priya@example.com",
    )

    assert first == same
    assert first == same_type_domain
    assert first != other_tenant
    assert first != other_kind
    assert len(first) == 64
    assert "priya" not in first


def test_suppression_digest_version_is_an_explicit_rotation_domain() -> None:
    old = SuppressionService(None, b"suppression-secret", key_version="2026-01")
    rotated = SuppressionService(None, b"suppression-secret", key_version="2026-07")

    assert old.digest(
        TENANT_A, "profile_url", "HTTPS://LinkedIn.com/in/Priya/?trk=x"
    ) != (
        rotated.digest(
            TENANT_A,
            "profile_url",
            "https://linkedin.com/in/Priya",
        )
    )


def test_profile_urls_and_phone_numbers_have_deterministic_normalization() -> None:
    service = SuppressionService(None, b"suppression-secret")

    assert service.digest(
        TENANT_A,
        "profile_url",
        "https://www.linkedin.com/in/priya/?trk=search",
    ) == service.digest(
        TENANT_A,
        "profile_url",
        "https://www.linkedin.com/in/priya",
    )
    assert service.digest(TENANT_A, "phone", "+1 (212) 555-0112") == service.digest(
        TENANT_A,
        "phone",
        "+12125550112",
    )
