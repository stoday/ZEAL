"""Context-local interaction boundary shared by terminal and agent workflows."""

from __future__ import annotations

import builtins
import getpass
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_current: ContextVar[Any] = ContextVar("zeal_interaction", default=None)


@contextmanager
def use_interaction(adapter: Any) -> Iterator[None]:
    token = _current.set(adapter)
    try:
        yield
    finally:
        _current.reset(token)


def input(prompt: str = "") -> str:
    adapter = _current.get()
    return adapter.ask(prompt) if adapter else builtins.input(prompt)


def secret_input(prompt: str) -> str:
    adapter = _current.get()
    return adapter.ask(prompt, secret=True) if adapter else getpass.getpass(prompt)


def print(*values: Any, **kwargs: Any) -> None:
    adapter = _current.get()
    if adapter:
        adapter.message(kwargs.get("sep", " ").join(map(str, values)))
    else:
        builtins.print(*values, **kwargs)


def choice(label: str, options: Any) -> str | None:
    adapter = _current.get()
    return adapter.ask(label, options=list(options)) if adapter else None


def notify(event: str, **fields: Any) -> None:
    adapter = _current.get()
    if adapter:
        adapter.notify(event, **fields)
