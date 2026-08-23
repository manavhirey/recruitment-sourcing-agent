from typing import Protocol

import jwt
from fastapi import HTTPException, status
from jwt import PyJWK, PyJWKClient, PyJWTError
from pydantic import ValidationError

from app.core.config import Settings
from app.identity.schemas import IdentityClaims


class JWKClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> PyJWK: ...


class TokenVerifier:
    def __init__(
        self, settings: Settings, jwks_client: JWKClient | None = None
    ) -> None:
        self._issuer = settings.oidc_issuer
        self._audience = settings.oidc_audience
        self._jwks_client = jwks_client or PyJWKClient(
            f"{settings.oidc_issuer.rstrip('/')}/.well-known/jwks.json",
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=300,
        )

    def verify(self, token: str) -> IdentityClaims:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            return IdentityClaims.model_validate(payload)
        except (PyJWTError, ValidationError, ValueError, TypeError) as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_token"},
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
