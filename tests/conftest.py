"""Shared pytest fixtures for Okareo MCP server tests."""

import time
from typing import Any
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def env_api_key(monkeypatch):
    """Set OKAREO_API_KEY in the environment."""
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


@pytest.fixture
def mock_okareo_client():
    """Create a mocked Okareo client instance."""
    with patch("src.okareo_client.Okareo") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance


# ---------------------------------------------------------------------------
# Auth fixtures (used by tests/unit/auth/* and tests/integration/*).
# In-process RSA keypair + JWKS so tests can sign and validate JWTs without
# a real Frontegg tenant.
# ---------------------------------------------------------------------------

_KID = "test-key-1"
_ISSUER = "https://test.frontegg.example"
_RESOURCE_SERVER = "http://localhost:8080"


@pytest.fixture(scope="session")
def rsa_keypair():
    """Generate an in-process RSA keypair once per test session."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    return {"private": private, "public": public}


@pytest.fixture(scope="session")
def jwks_doc(rsa_keypair) -> dict[str, Any]:
    """A JWKS document with the test keypair's public half, kid=test-key-1."""
    public_numbers = rsa_keypair["public"].public_numbers()

    def _b64url_uint(n: int) -> str:
        import base64

        as_bytes = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(as_bytes).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _KID,
                "n": _b64url_uint(public_numbers.n),
                "e": _b64url_uint(public_numbers.e),
            }
        ]
    }


@pytest.fixture
def jwt_signer(rsa_keypair):
    """Returns a callable that signs a payload dict with the fixture private key.

    Usage:
        token = jwt_signer({"aud": "...", "iss": "...", "exp": ..., "organization_id": "org-A"})
    """
    pem = rsa_keypair["private"].private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def _sign(payload: dict[str, Any], kid: str = _KID) -> str:
        return pyjwt.encode(
            payload, pem, algorithm="RS256", headers={"kid": kid}
        )

    return _sign


@pytest.fixture
def default_claims() -> dict[str, Any]:
    """A well-formed claims dict — tests override individual fields as needed."""
    now = int(time.time())
    return {
        "iss": _ISSUER,
        "aud": _RESOURCE_SERVER,
        "sub": "user-123",
        "exp": now + 600,
        "iat": now,
        "nbf": now,
        "organization_id": "org-A",
        "scope": "okareo:use",
    }


@pytest.fixture(scope="session")
def issuer_url() -> str:
    return _ISSUER


@pytest.fixture(scope="session")
def resource_server_url() -> str:
    return _RESOURCE_SERVER
