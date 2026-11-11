"""Isolation for independent input reads, never for authority or output writes.

Callers choose the smallest independently retainable unit and the data-error
types at its input boundary. Failed results must not be committed. Exceptions
outside that boundary (policy, identity, locks, persistence) always propagate.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field


class ItemAuthorityError(ValueError):
    """A binding/authority refusal, including inside an item read boundary."""


@dataclass(frozen=True)
class ItemFailure:
    item: str
    reason_code: str
    error_type: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ItemBatch:
    failures: list[ItemFailure] = field(default_factory=list)

    @contextmanager
    def isolate(
        self, item: str, *, errors: tuple[type[Exception], ...] = (OSError, ValueError),
    ) -> Iterator[None]:
        try:
            yield
        except ItemAuthorityError:
            raise
        except errors as exc:
            code = ("ITEM_DISAPPEARED" if isinstance(exc, FileNotFoundError) else
                    "ITEM_UNREADABLE" if isinstance(exc, OSError) else "ITEM_INVALID")
            self.failures.append(ItemFailure(item, code, type(exc).__name__, str(exc)))

    def as_list(self) -> list[dict[str, str]]:
        return [failure.as_dict() for failure in self.failures]
