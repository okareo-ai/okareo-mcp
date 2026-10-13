"""Run plain-function tools on worker threads, so no tool holds the event loop.

The MCP SDK calls a plain-function tool directly on the event loop
(``func_metadata.py``: ``return fn(**arguments_parsed_dict)``), and while it
runs nobody else is served: not sign-in, not the discovery documents, not
another person's tool call. ``ToolThreadsMixin.add_tool`` wraps every such
tool once, where it is registered, so a tool written as ``def`` runs on a
worker thread and the loop only waits for its answer. Tool code doesn't change.

The wrapper keeps the tool's published schema: FastMCP reads signatures with
``inspect.signature(..., eval_str=True)`` and finds the Context parameter with
``typing.get_type_hints``, and both follow ``__wrapped__`` back to the original
function and its module's globals.

MCP SDK 2.x runs plain tools on worker threads itself; when the server moves
to it, remove this module and the mixin from the server class.
"""

import functools
import inspect
import os
from typing import Any, Callable

import anyio
import anyio.to_thread
from anyio.lowlevel import RunVar

# Cloud Run sends an instance at most 80 requests (containerConcurrency) and a
# request makes at most one tool call, so with 80 threads no tool call ever
# waits for one. The deploy sets OKAREO_TOOL_THREADS to the same number.
_DEFAULT_TOOL_THREADS = 80

# Marks a wrapped tool, so a test can check every plain tool is wrapped.
RUNS_ON_THREAD = "__okareo_runs_on_thread__"

# One limiter per event loop, kept the way anyio keeps its own default
# limiter: a limiter is bound to the loop it was created in, and each test
# that calls asyncio.run starts a new loop.
_tool_limiter: RunVar[anyio.CapacityLimiter] = RunVar("okareo_tool_limiter")


class ToolThreadsMixin:
    """Registers every plain-function tool to run on a worker thread.

    Listed before the FastMCP class it is combined with, so its ``add_tool``
    runs first. Everything but the function is passed through unchanged, so a
    later MCP SDK that adds a parameter keeps working.
    """

    def add_tool(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return super().add_tool(run_on_thread(fn), *args, **kwargs)  # type: ignore[misc]



def run_on_thread(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a plain-function tool so it runs on a worker thread; return an
    async tool unchanged.

    The thread isn't abandoned if the caller is cancelled (anyio's default),
    so the limiter's count always matches the threads really running.
    """
    if is_async_tool(fn):
        return fn

    @functools.wraps(fn)
    async def runs_on_thread(*args: Any, **kwargs: Any) -> Any:
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs), limiter=tool_limiter()
        )

    setattr(runs_on_thread, RUNS_ON_THREAD, True)
    return runs_on_thread



def busy_tool_threads() -> int | None:
    """Tool calls running on worker threads right now, or None outside a loop."""
    try:
        return int(tool_limiter().borrowed_tokens)
    except Exception:
        return None



def tool_limiter() -> anyio.CapacityLimiter:
    """The current event loop's limiter for tool calls.

    Tool calls never use anyio's shared default pool (40 threads), which
    Starlette uses to serve the landing page and the sign-in page's files.
    """
    try:
        return _tool_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(tool_thread_cap())
        _tool_limiter.set(limiter)
        return limiter



def tool_thread_cap() -> int:
    """How many tool calls may run on worker threads at once in this process."""
    raw = os.environ.get("OKAREO_TOOL_THREADS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TOOL_THREADS
    return value if value > 0 else _DEFAULT_TOOL_THREADS



def is_async_tool(fn: Any) -> bool:
    """Whether FastMCP would await this tool. Mirrors the MCP SDK's own rule
    (``tools/base.py`` ``_is_async_callable``): look through
    ``functools.partial``, and count an object with an async ``__call__``."""
    while isinstance(fn, functools.partial):
        fn = fn.func
    return inspect.iscoroutinefunction(fn) or (
        callable(fn) and inspect.iscoroutinefunction(getattr(fn, "__call__", None))
    )
