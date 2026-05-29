"""Authentication subsystem for the Okareo MCP server (remote HTTP mode).

This package is loaded only when the server runs in HTTP mode (`TRANSPORT=
streamable-http`). The stdio transport path does not import from `src.auth`.

The public symbols defined here form a stable import surface for callers
(tools, server wiring, tests). Concrete implementations live in submodules.
"""

from src.auth.api_key_verifier import OkareoAPIKeyVerifier
from src.auth.context import (
    CredentialMissingError,
    SessionCredential,
    get_session_credential,
    get_session_credential_optional,
    set_session_credential,
)
from src.auth.dcr_proxy import build_dcr_app
from src.auth.jwks_cache import JWKSCache
from src.auth.verifier import CombinedTokenVerifier

__all__ = [
    "CombinedTokenVerifier",
    "CredentialMissingError",
    "JWKSCache",
    "OkareoAPIKeyVerifier",
    "SessionCredential",
    "build_dcr_app",
    "get_session_credential",
    "get_session_credential_optional",
    "set_session_credential",
]
