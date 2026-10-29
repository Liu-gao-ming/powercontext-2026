# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bounded code diagnostics without source text, query text or filesystem paths."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from powercontext._logging import error_category, log_safely
from powercontext.builtin.code.errors import CodeError

if TYPE_CHECKING:
    from powercontext.builtin.runtime.protocols import RuntimeTracing, TraceAttribute

_tracing: ContextVar[RuntimeTracing | None] = ContextVar("code_tracing", default=None)
_logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_T = TypeVar("_T")


@contextmanager
def tracing_context(tracing: RuntimeTracing | None) -> Iterator[None]:
    token = _tracing.set(tracing)
    try:
        yield
    finally:
        _tracing.reset(token)


@contextmanager
def stage(name: str) -> Iterator[dict[str, TraceAttribute]]:
    """Publish aggregate diagnostics; a broken observer cannot break a code operation."""
    started = time.monotonic()
    attributes: dict[str, TraceAttribute] = {}
    manager = span = None
    tracing = _tracing.get()
    if tracing is not None:
        with suppress(Exception):
            candidate = tracing.stage("code." + name, attributes={})
            span = candidate.__enter__()
            manager = candidate
    outcome = "success"
    diagnostic_code: str | None = None
    status_code: int | None = None
    try:
        yield attributes
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException as error:
        outcome = "failure"
        diagnostic_code = error.code if isinstance(error, CodeError) else type(error).__name__
        status_code = error.status if isinstance(error, CodeError) else None
        attributes["reason"] = diagnostic_code
        raise
    finally:
        attributes["seconds"] = time.monotonic() - started
        attributes["outcome"] = outcome
        if name in {"index", "query", "clear"}:
            # These are process-lifetime high-water marks, not one request's allocation
            # or a simultaneous process-tree peak. Never add them together.
            with suppress(ImportError, OSError):
                import resource

                scale = 1 if sys.platform == "darwin" else 1024
                attributes["process_lifetime_max_rss_bytes"] = (
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
                )
                attributes["children_lifetime_max_rss_bytes"] = (
                    resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
                )
        category = error_category(diagnostic_code, status_code=status_code, outcome=outcome)
        if span is not None:
            with suppress(Exception):
                span.set_attributes({"powercontext.code." + key: value for key, value in attributes.items()})
                if category is not None:
                    span.set_attributes({"powercontext.error.category": category})
                span.set_outcome(outcome)
        if manager is not None:
            with suppress(Exception):
                manager.__exit__(None, None, None)
        log_safely(
            _logger,
            logging.DEBUG,
            "Code stage completed",
            extra={
                **attributes,
                "code_operation": attributes.get("operation"),
                "event": "code." + name,
                "unit": "stage",
                "operation": "code." + name,
                "duration_ms": float(attributes["seconds"]) * 1000,
                "error_code": diagnostic_code,
                "status_code": status_code,
                "error_category": category,
            },
        )


def observed(name: str) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    def decorate(function: Callable[_P, _T]) -> Callable[_P, _T]:
        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            with stage(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate
