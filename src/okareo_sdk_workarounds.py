"""Make up for two gaps in the released Okareo SDK that the server depends on.

Written against okareo 0.0.159 (``okareo/async_utils.py``, ``okareo/okareo.py``),
and the server's only reliance on that SDK's internals:

- Every ``ModelUnderTest`` starts a datapoint thread when it is built, which
  runs until ``flush()``; ``flush()`` prints "Shutting down" to stdout, the
  stdio transport's protocol channel. ``close_model`` stops it without printing.
- ``Okareo(timeout=...)`` is ignored: the constructor builds its API client
  with no timeout and makes its first request on it. ``OkareoWithTimeout``
  gives that client the timeout before that request.

Both prefer the SDK's own behaviour when the installed SDK has it (a model's
``close()``; a constructor that honours ``timeout``), so a release that closes
these gaps needs no change here (spec 049, research R15).
"""

import logging
import threading
import httpx
from okareo import Okareo
from okareo.common import BASE_URL
from okareo.model_under_test import ModelUnderTest
from okareo_api_client import Client

_logger = logging.getLogger(__name__)

# How long close_model waits for the thread to stop. The server never queues
# datapoints, so the thread has nothing to deliver and stops at once; past
# this, it stops on its own at its next check of `done`.
_STOP_JOIN_TIMEOUT_SECONDS = 5.0


def close_model(model: ModelUnderTest) -> None:
    """Stop the model's datapoint thread without printing. Never raises.

    Calls the model's own ``close()`` when the installed SDK has one. 0.0.159
    has none, so this does what its ``flush()`` does, without the print: set
    ``done`` under the thread's condition, wake the thread, and join it
    (``async_utils.py`` lines 65–90). Nothing is lost from its queue: only
    ``add_data_point_async`` fills it, and the server never calls that.

    A model that can't be closed is logged and left running, not turned into
    the tool's error: by the time a model is closed, its Run has started.
    """
    try:
        close = getattr(model, "close", None)
        if callable(close):
            close()
            return
        thread = getattr(model, "worker_thread", None)
        if thread is None:
            return
        with model.condition:
            model.done = True
            model.condition.notify_all()
        if thread is not threading.current_thread():
            thread.join(_STOP_JOIN_TIMEOUT_SECONDS)
    except Exception:
        _logger.warning("Could not stop a model's datapoint thread", exc_info=True)


class OkareoWithTimeout(Okareo):
    """An ``Okareo`` client whose every request gives up after ``timeout``
    seconds, the constructor's own included.

    0.0.159 accepts ``timeout`` and ignores it: its constructor builds the
    generated ``Client`` with none and requests ``GET /v0/projects`` on it
    before returning (``okareo.py`` lines 195–201). This class gives that
    ``Client`` the timeout as the constructor assigns it, through the client's
    own ``with_timeout``; the client hasn't built its HTTP client yet, so the
    timeout reaches that request and every later one. On an SDK that honours
    ``timeout``, this sets the value the constructor already set.
    """

    def __init__(
        self, *, api_key: str, timeout: float, base_path: str = BASE_URL
    ) -> None:
        # Set before the parent constructor runs: its first assignment of
        # `client` reads it.
        self._request_timeout = httpx.Timeout(timeout)
        super().__init__(api_key=api_key, base_path=base_path, timeout=timeout)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "client" and isinstance(value, Client):
            value = value.with_timeout(self._request_timeout)
        super().__setattr__(name, value)
