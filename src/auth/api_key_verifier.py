"""Validates Okareo API keys for the bearer-fallback auth path.

Uses the Okareo SDK's existing key-validation flow (constructor calls
``GET /v0/projects``). Translates SDK exceptions into a clean
``SessionCredential | None`` return shape:

- Valid key → ``SessionCredential(kind="api_key", api_key, org_id)``
- Invalid key (SDK raises ``TypeError``) → ``None`` (verifier yields 401)
- Network / 5xx errors → raise (verifier yields 502)

Note on ``org_id``: the public Okareo API doesn't expose an organization
endpoint, so we use the first project's id as a stable per-account
identifier. This satisfies analytics + throttling without leaking the
credential. Frontegg JWTs carry a real ``organization_id`` claim, so the
OAuth path doesn't share this constraint.
"""

from __future__ import annotations

import asyncio
import logging

from okareo import Okareo

from src.auth.context import SessionCredential


_logger = logging.getLogger(__name__)


class OkareoAPIKeyVerifier:
    """Resolves an Okareo API key to a ``SessionCredential``."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    async def verify(self, api_key: str) -> SessionCredential | None:
        """Return a ``SessionCredential`` for ``api_key`` or None if invalid.

        Raises:
            httpx.HTTPError or transport-layer exceptions if the Okareo API
            is unreachable. The caller decides whether to translate to 401
            or 502.
        """
        # The Okareo SDK constructor is synchronous and does network I/O.
        # Run it in a thread to avoid blocking the event loop.
        return await asyncio.to_thread(self._verify_sync, api_key)

    def _verify_sync(self, api_key: str) -> SessionCredential | None:
        try:
            okareo = Okareo(api_key=api_key, base_path=self._base_url)
            projects = okareo.get_projects()
        except TypeError as exc:
            # SDK convention: TypeError == invalid key.
            _logger.warning(
                "API-key validation: Okareo SDK rejected the key against "
                "base_url=%s (TypeError: %s)",
                self._base_url,
                exc,
            )
            return None

        if not projects:
            # Valid key but no projects → cannot derive org_id.
            _logger.warning(
                "API-key auth succeeded but the account has no projects; "
                "cannot derive org_id — rejecting session."
            )
            return None

        org_id = str(projects[0].id)
        return SessionCredential(
            kind="api_key",
            api_key=api_key,
            org_id=org_id,
        )
