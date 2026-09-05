"""Scoped sealed-input transport for the fixed Qixi successor chain."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_SEALED_CHAT_AUTHORITY_BYTES: ContextVar[bytes | None] = ContextVar(
    "qixi_sealed_chat_authority_bytes", default=None
)


@contextmanager
def sealed_chat_authority_replay(payload: bytes | None) -> Iterator[None]:
    """Confine a hash-bound predecessor chat to one journal validation call."""

    token = _SEALED_CHAT_AUTHORITY_BYTES.set(payload)
    try:
        yield
    finally:
        _SEALED_CHAT_AUTHORITY_BYTES.reset(token)


def sealed_chat_authority_bytes() -> bytes | None:
    """Return the scoped predecessor input, never a process-global override."""

    return _SEALED_CHAT_AUTHORITY_BYTES.get()
