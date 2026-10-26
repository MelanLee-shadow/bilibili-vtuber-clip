"""Focused regression tests for soft supplement-audio pressure accounting."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.autoslice.supplement_audio_budget import (
    ATTEMPT_CAP,
    BUDGET_CONFIG_INVALID,
    BUDGET_CONFIG_MISMATCH,
    MAX_WINDOW_MS,
    TOTAL_AUDIO_CAP,
    WINDOW_CAP,
    WINDOW_TOO_LONG,
    BudgetConfigurationError,
    BudgetExceeded,
    ensure_budget,
    start_budget,
)


MODEL = "synthetic-audio-model"


def _dispatch_and_seal(budget, start_ms: int, end_ms: int) -> int:
    attempt_id = budget.consume("agy", MODEL, start_ms, end_ms)
    budget.finish_attempt(attempt_id, status="OBSERVED")
    return attempt_id


def test_default_pressure_targets_do_not_reject_new_or_repeated_dispatches(
    tmp_path: Path,
) -> None:
    budget = start_budget(
        tmp_path / "source.mp4",
        max_windows=1,
        max_audio_ms=1_000,
    )

    _dispatch_and_seal(budget, 0, 1_000)
    # A new source window and a repeated same-provider/window request both cross
    # historical pressure targets, but remain actual ledger attempts by default.
    _dispatch_and_seal(budget, 1_000, 2_000)
    repeated = budget.consume(
        "agy",
        MODEL,
        0,
        1_000,
        max_attempts_per_window=1,
    )
    budget.finish_attempt(repeated, status="FAILED", reason_code="SYNTHETIC")

    snapshot = budget.snapshot()
    assert snapshot["attempt_count"] == 3
    assert snapshot["distinct_window_count"] == 2
    assert snapshot["total_audio_ms"] == 3_000
    assert snapshot["refusal_count"] == 0
    assert snapshot["remaining_windows"] == 0
    assert snapshot["remaining_audio_ms"] == 0
    assert snapshot["failed_attempt_count"] == 1


def test_explicit_enforcement_preserves_window_and_total_refusals(
    tmp_path: Path,
) -> None:
    window_budget = start_budget(
        tmp_path / "window-source.mp4",
        max_windows=1,
        max_audio_ms=10_000,
        enforce_limits=True,
    )
    _dispatch_and_seal(window_budget, 0, 1_000)
    with pytest.raises(BudgetExceeded) as window_error:
        window_budget.consume("agy", MODEL, 1_000, 2_000)
    assert window_error.value.reason_code == WINDOW_CAP
    assert window_budget.snapshot()["attempt_count"] == 1

    total_budget = start_budget(
        tmp_path / "total-source.mp4",
        max_windows=3,
        max_audio_ms=1_000,
        enforce_limits=True,
    )
    _dispatch_and_seal(total_budget, 0, 1_000)
    with pytest.raises(BudgetExceeded) as total_error:
        total_budget.consume("agy", MODEL, 0, 1_000)
    assert total_error.value.reason_code == TOTAL_AUDIO_CAP
    assert total_budget.snapshot()["attempt_count"] == 1


def test_explicit_enforcement_can_retain_same_window_attempt_guard(
    tmp_path: Path,
) -> None:
    budget = start_budget(
        tmp_path / "attempt-source.mp4",
        max_windows=3,
        max_audio_ms=10_000,
        enforce_limits=True,
    )
    _dispatch_and_seal(budget, 0, 1_000)

    with pytest.raises(BudgetExceeded) as error:
        budget.consume(
            "agy",
            MODEL,
            0,
            1_000,
            max_attempts_per_window=1,
        )
    assert error.value.reason_code == ATTEMPT_CAP
    assert budget.snapshot()["attempt_count"] == 1


def test_complete_range_can_cross_engineering_window_target(tmp_path: Path) -> None:
    budget = start_budget(tmp_path / "complete-range-source.mp4")
    budget.consume("moss", MODEL, 0, 31_270)
    budget.record_cache_hit(provider="moss", model=MODEL, start_ms=0, end_ms=31_270)
    assert budget.snapshot()["total_audio_ms"] == 31_270
    assert budget.snapshot()["attempt_count"] == 1
    assert budget.snapshot()["cache_hit_count"] == 1


def test_explicitly_enforced_window_target_remains_hard(tmp_path: Path) -> None:
    budget = start_budget(tmp_path / "explicit-limit-source.mp4", enforce_limits=True)

    with pytest.raises(BudgetExceeded) as error:
        budget.consume("agy", MODEL, 0, MAX_WINDOW_MS + 1)

    assert error.value.reason_code == WINDOW_TOO_LONG
    assert budget.snapshot()["attempt_count"] == 0


def test_cache_hit_is_free_and_does_not_change_attempt_ledger(tmp_path: Path) -> None:
    budget = start_budget(tmp_path / "cache-source.mp4")
    budget.record_cache_hit(
        provider="agy",
        model=MODEL,
        start_ms=0,
        end_ms=1_000,
    )

    snapshot = budget.snapshot()
    assert snapshot["cache_hit_count"] == 1
    assert snapshot["attempt_count"] == 0
    assert snapshot["total_audio_ms"] == 0


def test_enforcement_mode_is_explicit_and_source_scoped(tmp_path: Path) -> None:
    source = tmp_path / "shared-source.mp4"
    budget = start_budget(source)
    assert ensure_budget(source) is budget

    with pytest.raises(BudgetConfigurationError) as error:
        ensure_budget(source, enforce_limits=True)
    assert error.value.reason_code == BUDGET_CONFIG_MISMATCH

    with pytest.raises(BudgetConfigurationError) as invalid:
        start_budget(tmp_path / "invalid-source.mp4", enforce_limits="yes")
    assert invalid.value.reason_code == BUDGET_CONFIG_INVALID
