"""Process-local ledger for additive audio evidence calls.

The ledger counts actual provider dispatches, not successful responses, and is
scoped to one resolved source path.  ``max_windows`` and ``max_audio_ms`` are
soft pressure targets by default; they remain in the snapshot for compatibility
and accounting, but they do not reject a new finding.  A caller with a real,
explicit fee or permission limit can opt into the old refusal behavior with
``enforce_limits=True``.  ``MAX_WINDOW_MS`` is also an engineering pressure
target; actual provider input limits belong to the provider client. Cache hits, typed provider failures and
pre-dispatch refusals are disclosed without fabricating a provider attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
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
BUDGET_CONFIG_INVALID = "LOCAL_AUDIO_BUDGET_CONFIG_INVALID"
BUDGET_CONFIG_MISMATCH = "LOCAL_AUDIO_BUDGET_CONFIG_MISMATCH"
ATTEMPT_CAP = "LOCAL_AUDIO_ATTEMPT_CAP"

_BUDGET_CONFIG_KEYS = frozenset({"max_windows", "max_audio_ms", "enforce_limits"})
_ATTEMPT_FINAL_STATUSES = frozenset(
    {"OBSERVED", "TEXT_UNLOCATED", "FAILED", "RESPONSE_REJECTED"}
)


class BudgetConfigurationError(ValueError):
    """A typed refusal to create a budget with unsafe or conflicting limits."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class BudgetExceeded(RuntimeError):
    """A sanitized refusal to start another additive audio attempt."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BudgetConfigurationError(
            BUDGET_CONFIG_INVALID,
            f"{field} must be a positive integer",
        )
    return value


def _validated_limits(
    *, max_windows: object = None, max_audio_ms: object = None
) -> tuple[int, int]:
    return (
        _positive_int(
            MAX_WINDOWS if max_windows is None else max_windows,
            field="max_windows",
        ),
        _positive_int(
            MAX_AUDIO_MS if max_audio_ms is None else max_audio_ms,
            field="max_audio_ms",
        ),
    )


def validate_budget_config(config: object) -> dict[str, int | bool] | None:
    """Validate a producer's optional local witness budget object.

    The per-window limit is intentionally not configurable: every explicit
    budget still uses the fixed 20-second exact-cue maximum.
    """

    if config is None:
        return None
    if not isinstance(config, Mapping):
        raise BudgetConfigurationError(
            BUDGET_CONFIG_INVALID,
            "local_audio_witness_budget must be an object",
        )
    unknown = set(config) - _BUDGET_CONFIG_KEYS
    if unknown:
        raise BudgetConfigurationError(
            BUDGET_CONFIG_INVALID,
            "local_audio_witness_budget only allows max_windows, max_audio_ms and enforce_limits",
        )
    max_windows, max_audio_ms = _validated_limits(
        max_windows=config.get("max_windows"),
        max_audio_ms=config.get("max_audio_ms"),
    )
    result = {"max_windows": max_windows, "max_audio_ms": max_audio_ms}
    if "enforce_limits" in config:
        if not isinstance(config["enforce_limits"], bool):
            raise BudgetConfigurationError(BUDGET_CONFIG_INVALID, "enforce_limits must be a boolean")
        result["enforce_limits"] = config["enforce_limits"]
    return result


class SupplementAudioBudget:
    """Account for dispatches and their sanitized terminal outcomes.

    The historical window and total-duration values are pressure targets unless
    ``enforce_limits`` is explicitly enabled.  This keeps the source-scoped
    ledger and its monotonic history while allowing a real unresolved finding to
    continue past an engineering default, including ``MAX_WINDOW_MS``.
    """

    def __init__(
        self,
        source_media: Path,
        *,
        max_windows: object = None,
        max_audio_ms: object = None,
        enforce_limits: object = False,
    ) -> None:
        self.source_media = source_media
        self.max_windows, self.max_audio_ms = _validated_limits(
            max_windows=max_windows,
            max_audio_ms=max_audio_ms,
        )
        if not isinstance(enforce_limits, bool):
            raise BudgetConfigurationError(
                BUDGET_CONFIG_INVALID,
                "enforce_limits must be a boolean",
            )
        self.enforce_limits = enforce_limits
        self._lock = threading.RLock()
        self._windows: dict[tuple[int, int], int] = {}
        self._total_audio_ms = 0
        self._attempt_count = 0
        self._attempts: list[dict[str, Any]] = []
        self._cache_hits: list[dict[str, Any]] = []
        self._refusals: list[dict[str, Any]] = []
        self._revision = 0
        self._account_history: dict | None = None

    @property
    def lock(self) -> threading.RLock:
        """Short counter/history transaction; never hold through provider I/O."""
        # Legacy owners acquire this property before consume(). Put the hard
        # admission wait before acquisition, never while holding the source lock.
        if self.enforce_limits:
            from src.autoslice.exact_review_parallel import wait_for_native_turn
            wait_for_native_turn()
        return self._lock

    def _record_refusal_locked(
        self,
        *,
        provider: str,
        model: str,
        start_ms: int,
        end_ms: int,
        reason_code: str,
    ) -> None:
        self._refusals.append(
            {
                "refusal_id": len(self._refusals) + 1,
                "provider": provider,
                "model": model,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": end_ms - start_ms,
                "reason_code": reason_code,
            }
        )
        self._revision += 1

    def record_refusal(
        self, *, provider: str, model: str, start_ms: int, end_ms: int, reason_code: str,
    ) -> None:
        """Append an owner's typed decision not to dispatch (an ordinary ledger event)."""

        if not isinstance(reason_code, str) or not reason_code:
            raise ValueError("refusal reason is invalid")
        with self.lock:
            self._record_refusal_locked(provider=provider, model=model, start_ms=start_ms,
                                        end_ms=end_ms, reason_code=reason_code)

    def consume(
        self,
        provider: str,
        model: str,
        start_ms: int,
        end_ms: int,
        *,
        max_attempts_per_window: int | None = None,
        last_native_admission: bool = False,
    ) -> int:
        """Reserve a dispatch; optionally declare this item's final request.

        A final reservation (including refusal) releases later hard-budget
        admissions before provider I/O. The caller must know that no subsequent
        native dispatch can be requested by this agenda item.
        """
        if type(last_native_admission) is not bool:
            raise TypeError("last_native_admission must be a boolean")
        if self.enforce_limits:
            from src.autoslice.exact_review_parallel import wait_for_native_turn
            wait_for_native_turn(require_open=True)
        try:
            return self._consume(provider, model, start_ms, end_ms,
                                 max_attempts_per_window=max_attempts_per_window)
        finally:
            if self.enforce_limits and last_native_admission:
                from src.autoslice.exact_review_parallel import finish_native_admission
                finish_native_admission()

    def _consume(
        self, provider: str, model: str, start_ms: int, end_ms: int, *,
        max_attempts_per_window: int | None,
    ) -> int:
        """Reserve one actual provider dispatch and return its stable id.

        The reservation is not rolled back when the provider fails or times
        out: an attempted request still spent the audio budget.  Provider and
        model identify the dispatch but do not make a repeated geometry window
        free for another provider.
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
        window = (start_ms, end_ms)
        with self.lock:
            pressure_reasons: list[str] = []
            account = self.snapshot()
            account_windows = {(r['start_ms'], r['end_ms']) for r in account['windows']}
            if duration_ms > MAX_WINDOW_MS:
                pressure_reasons.append(WINDOW_TOO_LONG)
            if window not in account_windows and len(account_windows) >= self.max_windows:
                pressure_reasons.append(WINDOW_CAP)
            if account['total_audio_ms'] + duration_ms > self.max_audio_ms:
                pressure_reasons.append(TOTAL_AUDIO_CAP)
            if max_attempts_per_window is not None:
                if type(max_attempts_per_window) is not int or max_attempts_per_window < 1:
                    raise ValueError("per-window attempt limit must be positive")
                prior_count = sum(
                    row["provider"] == provider and row["model"] == model
                    and (row["start_ms"], row["end_ms"]) == window
                    for row in account["attempts"]
                )
                if prior_count >= max_attempts_per_window:
                    pressure_reasons.append(ATTEMPT_CAP)
            if self.enforce_limits and pressure_reasons:
                self._record_refusal_locked(
                    provider=provider,
                    model=model,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    reason_code=pressure_reasons[0],
                )
                messages = {
                    WINDOW_TOO_LONG: "audio window exceeds the explicitly enforced local limit",
                    WINDOW_CAP: "audio window limit is exhausted",
                    TOTAL_AUDIO_CAP: "audio duration limit is exhausted",
                    ATTEMPT_CAP: "audio retry limit is exhausted",
                }
                raise BudgetExceeded(
                    pressure_reasons[0],
                    messages[pressure_reasons[0]],
                )
            attempt_id = self._attempt_count + 1
            self._windows[window] = self._windows.get(window, 0) + 1
            self._total_audio_ms += duration_ms
            self._attempt_count = attempt_id
            self._attempts.append(
                {
                    "attempt_id": attempt_id,
                    "provider": provider,
                    "model": model,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "duration_ms": duration_ms,
                    "status": "DISPATCHED",
                    "reason_code": None,
                    "http_status": None,
                }
            )
            self._revision += 1
            return attempt_id

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        reason_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """Seal one dispatched attempt with a typed, sanitized outcome."""

        if (
            isinstance(attempt_id, bool)
            or not isinstance(attempt_id, int)
            or attempt_id <= 0
            or status not in _ATTEMPT_FINAL_STATUSES
            or (reason_code is not None and not isinstance(reason_code, str))
            or (
                http_status is not None
                and (
                    isinstance(http_status, bool)
                    or not isinstance(http_status, int)
                    or not 100 <= http_status <= 599
                )
            )
        ):
            raise ValueError("native audio attempt outcome is invalid")

        with self.lock:
            if attempt_id > len(self._attempts):
                raise ValueError("native audio attempt id is unknown")
            row = self._attempts[attempt_id - 1]
            if row.get("attempt_id") != attempt_id:
                raise RuntimeError("native audio attempt ledger is inconsistent")
            expected = {
                "status": status,
                "reason_code": reason_code,
                "http_status": http_status,
            }
            if row.get("status") != "DISPATCHED":
                if all(row.get(key) == value for key, value in expected.items()):
                    return
                raise RuntimeError("native audio attempt outcome is already sealed")
            row.update(expected)
            self._revision += 1

    def record_cache_hit(
        self,
        *,
        provider: str,
        model: str,
        start_ms: int,
        end_ms: int,
    ) -> None:
        """Disclose a validated cache hit without consuming provider budget."""

        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("audio provider is invalid")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("audio model is invalid")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise ValueError("cached audio window is invalid")
        with self.lock:
            self._cache_hits.append(
                {
                    "cache_hit_id": len(self._cache_hits) + 1,
                    "provider": provider,
                    "model": model,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "duration_ms": end_ms - start_ms,
                }
            )
            self._revision += 1

    def restore_validated_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Restore a receipt-validated history; never reset limits or refund attempts.

        The canonical receipt consumer validates the self-hash, event structure,
        source content and monotonic history under its cross-process lock first.
        Build a temporary object so a failed restore cannot partially change us.
        """
        with self.lock:
            if (snapshot.get("source_media") != str(self.source_media)
                    or snapshot.get("max_windows") != self.max_windows
                    or snapshot.get("max_audio_ms") != self.max_audio_ms
                    or snapshot.get("enforce_limits", False) != self.enforce_limits):
                raise ValueError("restored budget source or limits differ")
            candidate = SupplementAudioBudget(
                self.source_media,
                max_windows=self.max_windows,
                max_audio_ms=self.max_audio_ms,
                enforce_limits=self.enforce_limits,
            )
            from src.autoslice.native_audio_history_reconciliation import active_snapshot, HISTORY_KEY
            full_snapshot = deepcopy(dict(snapshot))
            candidate._account_history = deepcopy(snapshot.get(HISTORY_KEY))
            snapshot = active_snapshot(snapshot)
            candidate._attempts = deepcopy(snapshot["attempts"])
            candidate._cache_hits = deepcopy(snapshot["cache_hits"])
            candidate._refusals = deepcopy(snapshot["refusals"])
            candidate._windows = {
                (row["start_ms"], row["end_ms"]): row["attempt_count"]
                for row in snapshot["windows"]
            }
            candidate._total_audio_ms = snapshot["total_audio_ms"]
            candidate._attempt_count = snapshot["attempt_count"]
            candidate._revision = snapshot["revision"]
            expected_snapshot = full_snapshot
            # Receipts written before ``enforce_limits`` was introduced omit
            # the mode field.  The receipt consumer validates those snapshots
            # as soft pressure and restores the same in-memory representation.
            expected_snapshot.setdefault("enforce_limits", False)
            if candidate.snapshot() != expected_snapshot:
                raise ValueError("restored budget counters differ")
            for name in ("_attempts", "_cache_hits", "_refusals", "_windows",
                         "_total_audio_ms", "_attempt_count", "_revision", "_account_history"):
                setattr(self, name, getattr(candidate, name))

    def snapshot(self) -> dict[str, Any]:
        """Return deterministic accounting without credentials or raw output."""

        with self.lock:
            windows = [
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "duration_ms": end_ms - start_ms,
                    "attempt_count": attempts,
                }
                for (start_ms, end_ms), attempts in sorted(self._windows.items())
            ]
            snapshot = {
                "source_media": str(self.source_media),
                "revision": self._revision,
                "max_windows": self.max_windows,
                "max_window_ms": MAX_WINDOW_MS,
                "max_audio_ms": self.max_audio_ms,
                "enforce_limits": self.enforce_limits,
                "distinct_window_count": len(self._windows),
                "total_audio_ms": self._total_audio_ms,
                "attempt_count": self._attempt_count,
                "sealed_attempt_count": sum(
                    row.get("status") != "DISPATCHED" for row in self._attempts
                ),
                "pending_attempt_count": sum(
                    row.get("status") == "DISPATCHED" for row in self._attempts
                ),
                "failed_attempt_count": sum(
                    row.get("status") in {"FAILED", "RESPONSE_REJECTED"}
                    for row in self._attempts
                ),
                "cache_hit_count": len(self._cache_hits),
                "refusal_count": len(self._refusals),
                "remaining_windows": max(
                    0,
                    self.max_windows - len(self._windows),
                ),
                "remaining_audio_ms": max(
                    0,
                    self.max_audio_ms - self._total_audio_ms,
                ),
                "windows": windows,
                "attempts": [dict(row) for row in self._attempts],
                "cache_hits": [dict(row) for row in self._cache_hits],
                "refusals": [dict(row) for row in self._refusals],
            }

            if self._account_history is not None:
                from src.autoslice.native_audio_history_reconciliation import pack_snapshot
                return pack_snapshot(snapshot, self._account_history)
            return snapshot


_REGISTRY_LOCK = threading.RLock()
_REGISTRY: dict[str, SupplementAudioBudget] = {}


def _resolved_source(source_media: os.PathLike[str] | str) -> Path:
    try:
        return Path(os.fspath(source_media)).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        raise TypeError("source_media must be a filesystem path") from None


def start_budget(
    source_media: os.PathLike[str] | str,
    *,
    max_windows: object = None,
    max_audio_ms: object = None,
    enforce_limits: object = False,
) -> SupplementAudioBudget:
    """Register a fresh source ledger.

    Historical ``max_windows``/``max_audio_ms`` values are soft by default.
    Set ``enforce_limits=True`` only when the caller has a real, explicit
    permission or fee limit to enforce.
    """

    resolved = _resolved_source(source_media)
    budget = SupplementAudioBudget(
        resolved,
        max_windows=max_windows,
        max_audio_ms=max_audio_ms,
        enforce_limits=enforce_limits,
    )
    with _REGISTRY_LOCK:
        _REGISTRY[str(resolved)] = budget
    return budget


def ensure_budget(
    source_media: os.PathLike[str] | str,
    *,
    max_windows: object = None,
    max_audio_ms: object = None,
    enforce_limits: object = None,
) -> SupplementAudioBudget:
    """Reuse an active source budget, or register one without resetting it.

    If either limit is explicitly supplied, both effective limits must match an
    existing budget.  ``enforce_limits=None`` means "do not change the active
    source ledger's mode"; a new ledger defaults to soft pressure.  A verifier
    built without an explicit config simply shares the active budget.
    """

    resolved = _resolved_source(source_media)
    has_explicit_limits = max_windows is not None or max_audio_ms is not None
    requested = (
        _validated_limits(
            max_windows=max_windows,
            max_audio_ms=max_audio_ms,
        )
        if has_explicit_limits
        else None
    )
    if enforce_limits is not None and not isinstance(enforce_limits, bool):
        raise BudgetConfigurationError(
            BUDGET_CONFIG_INVALID,
            "enforce_limits must be a boolean",
        )
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(str(resolved))
        if existing is not None:
            if requested is not None and (
                existing.max_windows,
                existing.max_audio_ms,
            ) != requested:
                raise BudgetConfigurationError(
                    BUDGET_CONFIG_MISMATCH,
                    "active source budget has different limits",
                )
            if enforce_limits is not None and existing.enforce_limits != enforce_limits:
                raise BudgetConfigurationError(
                    BUDGET_CONFIG_MISMATCH,
                    "active source budget has a different enforcement mode",
                )
            return existing
        if requested is None:
            requested = (MAX_WINDOWS, MAX_AUDIO_MS)
        budget = SupplementAudioBudget(
            resolved,
            max_windows=requested[0],
            max_audio_ms=requested[1],
            enforce_limits=False if enforce_limits is None else enforce_limits,
        )
        _REGISTRY[str(resolved)] = budget
        return budget


def get_budget(
    source_media: os.PathLike[str] | str,
) -> SupplementAudioBudget | None:
    """Return the active budget for a source, or ``None`` for legacy routes."""

    resolved = _resolved_source(source_media)
    with _REGISTRY_LOCK:
        return _REGISTRY.get(str(resolved))


AudioSupplementBudget = SupplementAudioBudget


__all__ = [
    "AudioSupplementBudget",
    "BUDGET_CONFIG_INVALID",
    "BUDGET_CONFIG_MISMATCH",
    "BudgetConfigurationError",
    "BudgetExceeded",
    "MAX_AUDIO_MS",
    "MAX_WINDOW_MS",
    "MAX_WINDOWS",
    "SupplementAudioBudget",
    "ensure_budget",
    "get_budget",
    "start_budget",
    "validate_budget_config",
]
