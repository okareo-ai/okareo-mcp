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


@pytest.fixture(autouse=True)
def _clear_active_project():
    """Reset the resolved-project context between tests (036).

    Production code always resolves inside ``@project_scoped`` or
    ``project_resolution_scope()``, both of which reset on exit. Tests that
    call ``resolve_project()`` directly have no such scope, so without this
    the last resolution would bleed into the next test.
    """
    from src.okareo_client import _active_project

    token = _active_project.set(None)
    try:
        yield
    finally:
        _active_project.reset(token)


@pytest.fixture
def sim_submission():
    """Capture what a simulation actually submits (036 revision 2).

    `run_simulation` no longer hands `okareo.run_simulation` a target name —
    the SDK would resolve that name through an unscoped lookup that cannot see
    a target in a non-Global project (research R13). It now resolves the
    target inside the acting project and builds the run itself, so assertions
    about what was submitted moved from that SDK call to `ModelUnderTest.run_test`.

    Yields the patched `run_test` mock. `simulation_params` on it carries the
    per-run knobs (repeats, max_turns, stop_check, ...); `checks` stays a
    top-level keyword.
    """
    from unittest.mock import MagicMock, patch

    default = MagicMock()
    default.id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    default.name = "some-target"
    default.project_id = "00000000-0000-0000-0000-000000000111"
    default.models = {"custom_endpoint": {"type": "custom_endpoint"}}

    def _resolve(okareo, name, project_id, kind="artifact"):
        """Translate a test's existing target mock into the listing shape.

        Tests written before revision 2 configure
        `okareo.get_target_by_name.return_value` with a TargetModelResponse
        shape (`.target` is the config dict). The resolver now returns the
        models-under-test listing shape (`.models` is `{type: config}`).
        Mapping one to the other keeps those tests asserting what they were
        written to assert, instead of forcing every voice test to be rewritten.
        """
        configured = getattr(getattr(okareo, "get_target_by_name", None), "return_value", None)
        config = getattr(configured, "target", None)
        if isinstance(config, dict):
            translated = MagicMock()
            translated.id = getattr(configured, "id", default.id)
            translated.name = getattr(configured, "name", name)
            translated.project_id = project_id
            translated.models = {config.get("type", "custom_endpoint"): config}
            return translated
        return default

    with patch(
        "src.tools.simulations.resolve_artifact_by_name", side_effect=_resolve
    ), patch("okareo.model_under_test.ModelUnderTest.run_test") as run_test:
        run_test.return_value = MagicMock(
            id="run-1", name="sim", app_link="https://app.okareo.com/r"
        )
        yield run_test
