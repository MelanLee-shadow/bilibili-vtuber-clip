"""Small process-local budget for additive audio evidence calls.

The budget counts attempted provider audio windows, not successful responses.
It is deliberately scoped to one resolved source path and has no persistence;
the caller starts a fresh instance at the beginning of each additive run.
"""

from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any


MAX_WINDOWS = 3
MAX_WINDOW_MS = 20_000
MAX_AUDIO_MS = 60_000

WINDOW_INVALID = "LOCAL_AUDIO_WINDOW_INVALID"
WINDOW_TOO_LONG = "LOCAL_AUDIO_WINDOW_TOO_LONG"
WINDOW_CAP = "LOCAL_AUDIO_WINDOW_CAP"
TOTAL_AUDIO_CAP = "LOCAL_AUDIO_TOTAL_CAP"
PROVIDER_INVALID = "LOCAL_AUDIO_PROVIDER_INVALID"


class BudgetExceeded(RuntimeError):
    """A sanitized refusal to start another additive audio attempt."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class SupplementAudioBudget:
    """Account for up to three distinct windows and 60 seconds of audio."""

    def __init__(self, source_media: Path) -> None:
        self.source_media = source_media
        self._lock = threading.RLock()
        self._windows: dict[tuple[int, int], int] = {}
        self._total_audio_ms = 0
        self._attempt_count = 0
        self._attempts: list[dict[str, Any]] = []

    @property
    def lock(self) -> threading.RLock:
        """Lock shared by cache rechecks and the provider call/save path."""

        return self._lock

    def consume(
        self,
        provider: str,
        model: str,
        start_ms: int,
        end_ms: int,
    ) -> None:
        """Reserve one actual provider attempt for a source-time window.

        The reservation is intentionally not rolled back when the provider
        fails or times out: an attempted request still spent the audio budget.
        ``provider`` and ``model`` identify the attempt for the caller but do
        not make a geometry window free for another provider.
        """

        if not isinstance(provider, str) or not provider.strip():
            raise BudgetExceeded(PROVIDER_INVALID, "audio provider is invalid")
        if not isinstance(model, str) or not model.strip():
            raise BudgetExceeded(PROVIDER_INVALID, "audio model is invalid")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise BudgetExceeded(WINDOW_INVALID, "audio window is invalid")
        duration_ms = end_ms - start_ms
        if duration_ms > MAX_WINDOW_MS:
            raise BudgetExceeded(WINDOW_TOO_LONG, "audio window exceeds the local limit")

        window = (start_ms, end_ms)
        with self._lock:
            if window not in self._windows and len(self._windows) >= MAX_WINDOWS:
                raise BudgetExceeded(WINDOW_CAP, "audio window limit is exhausted")
            if self._total_audio_ms + duration_ms > MAX_AUDIO_MS:
                raise BudgetExceeded(TOTAL_AUDIO_CAP, "audio duration limit is exhausted")
            self._windows[window] = self._windows.get(window, 0) + 1
            self._total_audio_ms += duration_ms
            self._attempt_count += 1
            self._attempts.append({"provider": provider, "model": model,
                                   "start_ms": start_ms, "end_ms": end_ms})

    def snapshot(self) -> dict[str, Any]:
        """Return serializable accounting without credentials or raw output."""

        with self._lock:
            windows = [
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "duration_ms": end_ms - start_ms,
                    "attempt_count": attempts,
                }
                for (start_ms, end_ms), attempts in self._windows.items()
            ]
            return {
                "source_media": str(self.source_media),
                "max_windows": MAX_WINDOWS,
                "max_window_ms": MAX_WINDOW_MS,
                "max_audio_ms": MAX_AUDIO_MS,
                "distinct_window_count": len(self._windows),
                "total_audio_ms": self._total_audio_ms,
                "attempt_count": self._attempt_count,
                "remaining_windows": max(0, MAX_WINDOWS - len(self._windows)),
                "remaining_audio_ms": max(0, MAX_AUDIO_MS - self._total_audio_ms),
                "windows": windows,
                "attempts": [dict(row) for row in self._attempts],
            }


_REGISTRY_LOCK = threading.RLock()
_REGISTRY: dict[str, SupplementAudioBudget] = {}


def _resolved_source(source_media: os.PathLike[str] | str) -> Path:
    try:
        return Path(os.fspath(source_media)).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        raise TypeError("source_media must be a filesystem path") from None


def start_budget(source_media: os.PathLike[str] | str) -> SupplementAudioBudget:
    """Register and return a fresh budget for one additive source run."""

    resolved = _resolved_source(source_media)
    budget = SupplementAudioBudget(resolved)
    with _REGISTRY_LOCK:
        _REGISTRY[str(resolved)] = budget
    return budget


def get_budget(source_media: os.PathLike[str] | str) -> SupplementAudioBudget | None:
    """Return the active budget for a source, or ``None`` for legacy routes."""

    resolved = _resolved_source(source_media)
    with _REGISTRY_LOCK:
        return _REGISTRY.get(str(resolved))


AudioSupplementBudget = SupplementAudioBudget


__all__ = [
    "AudioSupplementBudget",
    "BudgetExceeded",
    "MAX_AUDIO_MS",
    "MAX_WINDOW_MS",
    "MAX_WINDOWS",
    "SupplementAudioBudget",
    "get_budget",
    "start_budget",
]
