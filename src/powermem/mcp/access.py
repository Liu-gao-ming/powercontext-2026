#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Request-scoped access logging for the PowerMem MCP server.

Implements the U5 / U6 contract semantics prepared for the VLDB powercontext
second question (official contract not yet published; this is pure prep work):

U5 - logical request_id bridging
    A logical MCP tool call receives one stable ``request_id`` (uuid hex)
    that is shared by the tool body (readable through
    :func:`current_request_id`) and by the access-log record emitted for the
    call. Internal bridge work may be flagged with
    :func:`mark_internal_bridge` / queried with :func:`is_internal_bridge`.

U6 - single-count access log
    Exactly one access-log record is emitted per logical tool call
    (``event="transport.request.completed"``, ``operation="mcp.tools.call"``,
    ``transport="mcp"``). Internal bridge / HTTP calls are never counted as
    additional tool calls; if a bridge emits its own transport log it must use
    its own ``transport`` value (e.g. ``"http"``) so the counts stay distinct.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import time
import uuid
from typing import Any, Callable, Optional, Tuple, TypeVar

access_logger = logging.getLogger("powermem.mcp.access")

_REQUEST_ID_VAR: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "powermem_mcp_request_id", default=None
)
_INTERNAL_VAR: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "powermem_mcp_internal", default=False
)


def begin_request() -> Tuple[str, contextvars.Token]:
    """Start a new logical request scope and return (request_id, reset_token).

    The caller must ``reset_request(reset_token)`` once the scope finishes so
    the context variables do not leak into unrelated code.
    """
    request_id = uuid.uuid4().hex
    token = _REQUEST_ID_VAR.set(request_id)
    _INTERNAL_VAR.set(False)
    return request_id, token


def reset_request(token: contextvars.Token) -> None:
    """End a request scope started by :func:`begin_request`."""
    _REQUEST_ID_VAR.reset(token)
    _INTERNAL_VAR.set(False)


def current_request_id() -> Optional[str]:
    """Return the request_id of the enclosing logical tool call, if any."""
    return _REQUEST_ID_VAR.get()


def mark_internal_bridge() -> None:
    """Flag the current request scope as an internal bridge invocation."""
    _INTERNAL_VAR.set(True)


def is_internal_bridge() -> bool:
    """Whether the current request scope is marked as an internal bridge."""
    return bool(_INTERNAL_VAR.get())


def log_access(
    *,
    operation: str,
    transport: str,
    request_id: Optional[str] = None,
    internal: bool = False,
    status: str = "ok",
    duration_ms: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    """Emit a structured access-log record.

    The record carries ``event="transport.request.completed"`` so U6 filters
    can reliably count logical tool calls and distinguish them from bridge /
    HTTP traffic.
    """
    data: dict[str, Any] = {
        "event": "transport.request.completed",
        "operation": operation,
        "transport": transport,
        "request_id": request_id if request_id is not None else current_request_id(),
        "internal": bool(internal),
        "status": status,
    }
    if duration_ms is not None:
        data["duration_ms"] = round(duration_ms, 3)
    if extra:
        data.update(extra)
    access_logger.info("powermem mcp access log", extra=data)


F = TypeVar("F", bound=Callable[..., Any])


def access_logged(
    operation: str = "mcp.tools.call",
    transport: str = "mcp",
) -> Callable[[F], F]:
    """Decorator wrapping an MCP tool with request_id + single-count logging.

    Usage (inside :mod:`powermem.mcp.server`)::

        @mcp.tool()
        @access_logged()
        def add_memory(...): ...

    The decorator must sit *under* ``@mcp.tool()`` so FastMCP registers the
    wrapped function (``functools.wraps`` preserves the original signature for
    schema generation).
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            request_id, token = begin_request()
            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                status = "ok"
            except Exception:
                status = "error"
                log_access(
                    operation=operation,
                    transport=transport,
                    request_id=request_id,
                    internal=is_internal_bridge(),
                    status=status,
                    duration_ms=(time.perf_counter() - start) * 1000.0,
                )
                raise
            else:
                log_access(
                    operation=operation,
                    transport=transport,
                    request_id=request_id,
                    internal=is_internal_bridge(),
                    status=status,
                    duration_ms=(time.perf_counter() - start) * 1000.0,
                )
            finally:
                reset_request(token)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
