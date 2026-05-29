"""Unit tests for src/auth/context.py (SessionCredential + ContextVar plumbing)."""

import asyncio

import pytest

from src.auth.context import (
    CredentialMissingError,
    SessionCredential,
    get_session_credential,
    get_session_credential_optional,
    set_session_credential,
)


def _make_cred(api_key: str, org_id: str) -> SessionCredential:
    return SessionCredential(kind="oauth", api_key=api_key, org_id=org_id)


class TestGetSessionCredential:
    def test_raises_when_no_credential_bound(self):
        # Run in a fresh context so we don't see leakage from other tests.
        import contextvars

        ctx = contextvars.copy_context()

        def _inner():
            with pytest.raises(CredentialMissingError):
                get_session_credential()

        ctx.run(_inner)

    def test_optional_returns_none_when_no_credential_bound(self):
        import contextvars

        ctx = contextvars.copy_context()

        def _inner():
            assert get_session_credential_optional() is None

        ctx.run(_inner)

    def test_set_get_roundtrip(self):
        import contextvars

        ctx = contextvars.copy_context()

        def _inner():
            cred = _make_cred("key-A", "org-A")
            set_session_credential(cred)
            assert get_session_credential() is cred
            assert get_session_credential_optional() is cred

        ctx.run(_inner)


class TestCredentialIsolation:
    """The whole point of the ContextVar: two concurrent tasks don't see each
    other's credential, even though they share the same process and the same
    underlying module-level ContextVar.
    """

    def test_two_concurrent_asyncio_tasks_see_their_own_credential(self):
        captured: dict[str, SessionCredential | None] = {}

        async def task(label: str, cred: SessionCredential, barrier: asyncio.Event):
            set_session_credential(cred)
            # Yield to the event loop to give the other task a chance to set
            # *its* credential before we read ours back — this is the test
            # that ContextVars actually isolate.
            await barrier.wait()
            captured[label] = get_session_credential()

        async def run():
            barrier = asyncio.Event()
            cred_a = _make_cred("key-A", "org-A")
            cred_b = _make_cred("key-B", "org-B")
            t_a = asyncio.create_task(task("A", cred_a, barrier))
            t_b = asyncio.create_task(task("B", cred_b, barrier))
            # Let both tasks reach the await; then release.
            await asyncio.sleep(0)
            barrier.set()
            await asyncio.gather(t_a, t_b)

        asyncio.run(run())

        assert captured["A"].api_key == "key-A"
        assert captured["A"].org_id == "org-A"
        assert captured["B"].api_key == "key-B"
        assert captured["B"].org_id == "org-B"


class TestSessionCredentialShape:
    def test_defaults(self):
        cred = SessionCredential(kind="api_key", api_key="k", org_id="o")
        assert cred.subject is None
        assert cred.expires_at is None
        assert cred.scopes == ("okareo:use",)

    def test_is_frozen(self):
        cred = SessionCredential(kind="oauth", api_key="k", org_id="o")
        with pytest.raises(Exception):
            # frozen=True dataclass raises FrozenInstanceError (subclass of
            # AttributeError); we don't care which, just that mutation fails.
            cred.api_key = "k2"  # type: ignore[misc]
