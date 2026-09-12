"""Focused tests for explicit native-audio budget configuration."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from src.autoslice import native_foreign_witness as native
from src.autoslice.native_foreign_witness import BudgetReceiptPersistenceError
from src.autoslice.producer_request import _validate_foreign_script_witness_provider
from src.autoslice.supplement_audio_budget import (
    BUDGET_CONFIG_INVALID,
    BUDGET_CONFIG_MISMATCH,
    WINDOW_CAP,
    BudgetConfigurationError,
    BudgetExceeded,
    get_budget,
    start_budget,
)


def _consume_four(budget) -> None:
    for index in range(4):
        budget.consume("moss", "moss-transcribe-diarize-pro", index * 1_000, (index + 1) * 1_000)


def test_default_budget_keeps_three_window_cap() -> None:
    budget = start_budget(Path("/tmp/native-budget-default-test"))
    for index in range(3):
        budget.consume("moss", "moss-transcribe-diarize-pro", index * 1_000, (index + 1) * 1_000)

    with pytest.raises(BudgetExceeded) as exc_info:
        budget.consume("moss", "moss-transcribe-diarize-pro", 3_000, 4_000)

    assert exc_info.value.reason_code == WINDOW_CAP
    assert budget.snapshot()["max_windows"] == 3


def test_explicit_budget_allows_fourth_window_then_rejects_fifth() -> None:
    budget = start_budget(
        Path("/tmp/native-budget-explicit-test"), max_windows=4, max_audio_ms=4_000
    )
    _consume_four(budget)

    with pytest.raises(BudgetExceeded) as exc_info:
        budget.consume("moss", "moss-transcribe-diarize-pro", 4_000, 5_000)

    assert exc_info.value.reason_code == WINDOW_CAP
    snapshot = budget.snapshot()
    assert snapshot["max_windows"] == 4
    assert snapshot["max_audio_ms"] == 4_000
    assert snapshot["attempt_count"] == 4


@pytest.mark.parametrize(
    "config",
    [
        True,
        [],
        {"max_windows": True},
        {"max_audio_ms": False},
        {"max_windows": 0},
        {"max_audio_ms": -1},
        {"unexpected": 4},
    ],
)
def test_invalid_explicit_budget_is_typed_config_error(config: object) -> None:
    with pytest.raises(BudgetConfigurationError) as exc_info:
        _validate_foreign_script_witness_provider(
            {
                "local_audio_witness_provider": "moss",
                "local_audio_witness_budget": config,
            }
        )

    assert exc_info.value.reason_code == BUDGET_CONFIG_INVALID


def test_budget_without_explicit_native_provider_is_rejected() -> None:
    with pytest.raises(BudgetConfigurationError) as exc_info:
        _validate_foreign_script_witness_provider(
            {"local_audio_witness_budget": {"max_windows": 4}}
        )

    assert exc_info.value.reason_code == BUDGET_CONFIG_INVALID


def test_native_verifier_reuses_active_budget_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable")
    first = native.build_native_foreign_witness(
        source_media=source,
        output_dir=tmp_path / "first",
        provider="moss",
        max_windows=4,
        max_audio_ms=4_000,
    )
    del first
    budget = get_budget(source)
    assert budget is not None
    budget.consume("moss", "moss-transcribe-diarize-pro", 0, 1_000)

    second = native.build_native_foreign_witness(
        source_media=source,
        output_dir=tmp_path / "second",
        provider="moss",
        max_windows=4,
        max_audio_ms=4_000,
    )
    del second
    assert get_budget(source) is budget
    assert budget.snapshot()["attempt_count"] == 1

    with pytest.raises(BudgetConfigurationError) as exc_info:
        native.build_native_foreign_witness(
            source_media=source,
            output_dir=tmp_path / "mismatch",
            provider="moss",
            max_windows=5,
            max_audio_ms=4_000,
        )
    assert exc_info.value.reason_code == BUDGET_CONFIG_MISMATCH
    assert budget.snapshot()["attempt_count"] == 1


def test_native_attempt_persists_limits_and_typed_attempt_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable")
    output = tmp_path / "out"
    monkeypatch.setattr(
        native,
        "_extract_exact_mp3",
        lambda _source, path, _start, _end: path.write_bytes(b"audio"),
    )

    def fail_provider(_audio, *, supplement_source, crop_start_ms, crop_end_ms, **_kwargs):
        budget = get_budget(supplement_source)
        assert budget is not None
        budget.consume(
            "moss",
            "moss-transcribe-diarize-pro",
            crop_start_ms,
            crop_end_ms,
        )
        raise BudgetExceeded(WINDOW_CAP, "synthetic cap refusal")

    monkeypatch.setattr(native, "observe_secondary", fail_provider)
    observe = native.build_native_foreign_witness(
        source_media=source,
        output_dir=output,
        provider="moss",
        max_windows=24,
        max_audio_ms=180_000,
    )

    with pytest.raises(BudgetExceeded) as exc_info:
        observe(start_ms=0, end_ms=1_000)

    assert exc_info.value.reason_code == WINDOW_CAP
    receipt = json.loads((output / "native-audio-budget.json").read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "native-audio-budget.v2"
    assert receipt["source_media_sha256"]
    assert receipt["budget"]["max_windows"] == 24
    assert receipt["budget"]["max_audio_ms"] == 180_000
    assert receipt["budget"]["attempt_count"] == 1
    assert receipt["budget"]["pending_attempt_count"] == 1
    assert receipt["budget"]["attempts"][0]["status"] == "DISPATCHED"
    assert receipt["budget"]["attempts"][0]["start_ms"] == 0


def test_budget_receipt_persistence_failure_cannot_return_provider_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable")
    monkeypatch.setattr(
        native,
        "_extract_exact_mp3",
        lambda _source, path, _start, _end: path.write_bytes(b"audio"),
    )
    audio_sha = hashlib.sha256(b"audio").hexdigest()
    monkeypatch.setattr(
        native,
        "observe_secondary",
        lambda *_args, **_kwargs: {
            "provider": "moss",
            "model": "moss-transcribe-diarize-pro",
            "input_audio_sha256": audio_sha,
            "response_sha256": "c" * 64,
            "native_segments": [{"start_ms": 0, "end_ms": 500, "text": "原话"}],
        },
    )

    def fail_receipt(**_kwargs):
        raise BudgetReceiptPersistenceError("synthetic persistence failure")

    observe = native.build_native_foreign_witness(
        source_media=source,
        output_dir=tmp_path / "out",
        provider="moss",
    )
    # Initial admission now persists an empty receipt. Inject failure after
    # construction to keep exercising the post-observation persistence seam.
    monkeypatch.setattr(native, "_persist_budget_receipt", fail_receipt)

    with pytest.raises(BudgetReceiptPersistenceError):
        observe(start_ms=0, end_ms=1_000)
