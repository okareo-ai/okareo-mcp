"""What the MCP does itself on the released Okareo SDK (049, research R15).

okareo 0.0.159 starts a datapoint thread for every ``ModelUnderTest`` that runs
until ``flush()``, which prints to stdout, and ignores ``Okareo(timeout=...)``.
These tests run against the installed release, so a release that changes how
it builds a model or a client fails here before it ships.
"""

import logging
from types import SimpleNamespace

import httpx
import pytest
from okareo import Okareo

from src.okareo_sdk_workarounds import OkareoWithTimeout, close_model


class TestCloseModel:
    def test_a_released_models_thread_is_stopped_without_printing(
        self, stand_in_model, capfd
    ):
        model = stand_in_model()
        thread = model.worker_thread
        # 0.0.159 starts the thread when the model is built, and has no way to
        # stop it but flush(), which prints "Shutting down".
        assert thread.is_alive()
        assert not hasattr(model, "close")

        close_model(model)

        assert not thread.is_alive()
        assert capfd.readouterr().out == ""

    def test_a_model_with_its_own_close_is_closed_by_it(self):
        calls = []

        def _join(timeout=None):
            pytest.fail("the fallback must not run when the model has close()")

        model = SimpleNamespace(
            close=lambda: calls.append("close"),
            condition=None,
            worker_thread=SimpleNamespace(join=_join),
        )
        close_model(model)
        assert calls == ["close"]

    def test_a_model_that_cannot_be_closed_is_logged_and_never_raised(self, caplog):
        def _refuse():
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="src.okareo_sdk_workarounds"):
            close_model(SimpleNamespace(close=_refuse))
        assert "datapoint thread" in caplog.text

    def test_a_model_with_no_thread_needs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.okareo_sdk_workarounds"):
            close_model(SimpleNamespace(worker_thread=None))
        assert caplog.text == ""


class TestOkareoWithTimeout:
    def test_the_constructors_own_request_and_every_later_one_carry_the_timeout(
        self, projects_request_timeouts
    ):
        okareo = OkareoWithTimeout(
            api_key="k", base_path="https://api.example", timeout=2.5
        )
        # 0.0.159 builds its client without the timeout and requests
        # GET /v0/projects on it before the constructor returns.
        assert projects_request_timeouts == [httpx.Timeout(2.5)]
        assert okareo.client.get_httpx_client().timeout == httpx.Timeout(2.5)
        assert isinstance(okareo, Okareo)

    def test_the_released_constructor_ignores_its_timeout(
        self, projects_request_timeouts
    ):
        """Why OkareoWithTimeout exists. When a release honours the timeout,
        this fails, and the subclass can go."""
        Okareo(api_key="k", base_path="https://api.example", timeout=2.5)
        assert projects_request_timeouts == [httpx.Timeout(None)]
