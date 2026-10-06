"""Authentication failures that carry a message for the client.

Raised by ``CombinedTokenVerifier`` for key-shaped bearers and rendered by
``OkareoFastMCP`` (specs/048-api-key-shared-connections research R6). They
live apart from the verifier so ``protected_resource`` can import them
without importing the verifier and everything it depends on.
"""

from __future__ import annotations

from starlette.authentication import AuthenticationError


class InvalidAPIKeyError(AuthenticationError):
    """The bearer is key-shaped and okareo-server (or the shape check) refused it.

    Raised rather than returned as ``None`` so the response can tell the user
    what to fix; ``OkareoFastMCP`` turns it into a 401. ``description`` goes
    to the client and must never contain credential material.
    """

    def __init__(
        self,
        description: str = (
            "This Okareo API key is not valid (revoked, expired, or unknown). "
            "Create or check your key at app.okareo.com."
        ),
    ) -> None:
        super().__init__(description)
        self.description = description


class CredentialUnavailableError(AuthenticationError):
    """okareo-server could not be asked whether a key is valid.

    Distinct from ``InvalidAPIKeyError`` so an outage never tells the user
    their key is wrong (FR-006); ``OkareoFastMCP`` turns it into a 503.
    """

    def __init__(
        self,
        description: str = (
            "Okareo could not be reached to check this API key. Retry shortly."
        ),
    ) -> None:
        super().__init__(description)
        self.description = description
