from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt import PyJWK

from app.core.config import Settings
from app.core.security import TokenVerifier
from app.identity.schemas import IdentityClaims


class StaticJWKClient:
    def __init__(self, signing_key: PyJWK) -> None:
        self.signing_key = signing_key

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        return self.signing_key


def _signed_token(overrides: dict[str, Any] | None = None) -> tuple[str, PyJWK]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": "oidc|owner-1",
        "email": "owner@agency.test",
        "name": "Owner",
        "email_verified": True,
        "iss": "https://issuer.test/",
        "aud": "sourcing-api",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    payload.update(overrides or {})
    token = jwt.encode(
        payload, private_key, algorithm="RS256", headers={"kid": "key-1"}
    )
    public_jwk = PyJWK.from_dict(
        jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    )
    return token, public_jwk


def test_identity_claims_requires_subject_and_email() -> None:
    claims = IdentityClaims.model_validate(
        {"sub": "oidc|owner-1", "email": "owner@agency.test", "name": "Owner"}
    )

    assert claims.subject == "oidc|owner-1"
    assert claims.email == "owner@agency.test"


def test_verifier_accepts_valid_rs256_identity_token() -> None:
    token, signing_key = _signed_token()
    verifier = TokenVerifier(Settings.for_test(), StaticJWKClient(signing_key))

    claims = verifier.verify(token)

    assert claims == IdentityClaims(
        subject="oidc|owner-1",
        email="owner@agency.test",
        name="Owner",
        email_verified=True,
    )


def test_verifier_maps_invalid_audience_to_stable_unauthorized_response() -> None:
    token, signing_key = _signed_token({"aud": "another-api"})
    verifier = TokenVerifier(Settings.for_test(), StaticJWKClient(signing_key))

    with pytest.raises(HTTPException) as error:
        verifier.verify(token)

    assert error.value.status_code == 401
    assert error.value.detail == {"code": "invalid_token"}
    assert error.value.headers == {"WWW-Authenticate": "Bearer"}
