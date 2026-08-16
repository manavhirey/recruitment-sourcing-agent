import base64
from dataclasses import replace
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag

from app.candidates.contacts import ContactCipher, ContactContext


@pytest.fixture
def cipher() -> ContactCipher:
    return ContactCipher(
        base64.b64encode(b"k" * 32).decode(),
        b"lookup-key-that-is-not-the-encryption-key",
    )


@pytest.fixture
def context() -> ContactContext:
    return ContactContext(uuid4(), uuid4(), "email")


def test_contact_cipher_round_trip_without_plaintext_storage(
    cipher: ContactCipher, context: ContactContext
) -> None:
    encrypted = cipher.encrypt("priya@example.com", context)

    assert b"priya@example.com" not in encrypted.ciphertext
    assert b"priya@example.com" not in encrypted.encrypted_data_key
    assert cipher.decrypt(encrypted, context) == "priya@example.com"


def test_contact_cipher_uses_random_per_contact_data_keys(
    cipher: ContactCipher, context: ContactContext
) -> None:
    first = cipher.encrypt("priya@example.com", context)
    second = cipher.encrypt("priya@example.com", context)

    assert first.ciphertext != second.ciphertext
    assert first.encrypted_data_key != second.encrypted_data_key
    assert first.lookup_hmac == second.lookup_hmac


def test_contact_cipher_rejects_ciphertext_and_wrapped_key_tampering(
    cipher: ContactCipher, context: ContactContext
) -> None:
    encrypted = cipher.encrypt("priya@example.com", context)

    with pytest.raises(InvalidTag):
        cipher.decrypt(
            encrypted.with_ciphertext(encrypted.ciphertext[:-1] + b"0"), context
        )
    with pytest.raises(InvalidTag):
        cipher.decrypt(
            encrypted.with_encrypted_data_key(encrypted.encrypted_data_key[:-1] + b"0"),
            context,
        )


@pytest.mark.parametrize("field", ["tenant", "candidate", "contact_type"])
def test_contact_cipher_authenticates_all_context_fields(
    cipher: ContactCipher, context: ContactContext, field: str
) -> None:
    encrypted = cipher.encrypt("priya@example.com", context)
    wrong = ContactContext(
        uuid4() if field == "tenant" else context.tenant_id,
        uuid4() if field == "candidate" else context.candidate_id,
        "phone" if field == "contact_type" else context.contact_type,
    )

    with pytest.raises(InvalidTag):
        cipher.decrypt(encrypted, wrong)


def test_contact_cipher_authenticates_schema_version(
    cipher: ContactCipher, context: ContactContext
) -> None:
    encrypted = cipher.encrypt("priya@example.com", context)

    with pytest.raises(InvalidTag):
        cipher.decrypt(replace(encrypted, schema_version=2), context)


def test_contact_lookup_hmac_is_normalized_and_tenant_keyed(
    cipher: ContactCipher, context: ContactContext
) -> None:
    same_tenant = ContactContext(context.tenant_id, uuid4(), "email")
    other_tenant = ContactContext(uuid4(), uuid4(), "email")

    first = cipher.lookup_hmac(" Priya@Example.COM ", context)
    same = cipher.lookup_hmac("priya@example.com", same_tenant)
    other = cipher.lookup_hmac("priya@example.com", other_tenant)

    assert first == same
    assert first != other


def test_contact_cipher_requires_a_256_bit_key() -> None:
    with pytest.raises(ValueError, match="256-bit"):
        ContactCipher(base64.b64encode(b"short").decode(), b"lookup")
