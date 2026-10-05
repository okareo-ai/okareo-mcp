"""Discovery helpers shared by the OAuth integration tests (046)."""

from __future__ import annotations

from urllib.parse import urlsplit


def authorization_server_metadata_url(issuer: str) -> str:
    """RFC 8414 §3.1: the metadata URL for an issuer identifier.

    ``/.well-known/oauth-authorization-server`` is inserted between the host
    and the issuer's path component, if any. The path is kept verbatim: an
    issuer of ``https://x/`` yields ``.../oauth-authorization-server/``. That
    is how a strict client (the Copilot CLI among them) builds the request,
    and it is the URL whose document must carry an identical ``issuer``.
    """
    parts = urlsplit(issuer)
    return (
        f"{parts.scheme}://{parts.netloc}"
        f"/.well-known/oauth-authorization-server{parts.path}"
    )
