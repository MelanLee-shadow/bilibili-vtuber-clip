from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import diarized_transcription
from src.autoslice import native_foreign_witness as native
from src.autoslice import subtitle_audio_evidence as secondary
from src.autoslice.diarized_transcription import DiarizedTranscriptionError
from src.autoslice.mai_transcription import MAI_MODEL
from src.autoslice.moss_transcription import MOSS_MODEL
from src.autoslice.native_audio_budget_receipt import (
    BudgetReceiptPersistenceError,
    persist_native_audio_budget_receipt,
    receipt_sha256,
)
from src.autoslice.supplement_audio_budget import (
    TOTAL_AUDIO_CAP,
    WINDOW_CAP,
    BudgetExceeded,
    get_budget,
    start_budget,
)


def _evidence(audio: bytes, *, provider: str) -> dict[str, object]:
    model = {"moss": MOSS_MODEL, "mai": MAI_MODEL}[provider]
    return {
        "provider": provider,
        "model": model,
        "status": "OBSERVED",
        "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
        "response_sha256": ("a" if provider == "moss" else "b") * 64,
        "native_segments": [
            {"start_ms": 0, "end_ms": 500, "text": "原话"}
        ],
        "native_timeline": {"valid": True},
        "raw_response": {"text": "原话"},
    }


def test_success_and_cache_hit_have_distinct_budget_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    media = tmp_path / "target.mp3"
    audio = b"audio"
    budget = start_budget(source)

    def transcribe(
        payload: bytes,
        *,
        provider: str,
        duration_ms: int,
        before_request,
    ) -> dict[str, object]:
        assert duration_ms == 1_000
        before_request()
        return _evidence(payload, provider=provider)

    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", transcribe)
    first = secondary.observe_secondary(
        audio,
        media_path=media,
        provider="moss",
        duration_ms=1_000,
        supplement_source=source,
        crop_start_ms=10_000,
        crop_end_ms=11_000,
        exact_cue=True,
    )
    assert first["served_from_cache"] is False
    first_snapshot = budget.snapshot()
    assert first_snapshot["attempt_count"] == 1
    assert first_snapshot["sealed_attempt_count"] == 1
    assert first_snapshot["pending_attempt_count"] == 0
    assert first_snapshot["attempts"][0]["status"] == "OBSERVED"
    assert first_snapshot["revision"] == 2

    warm = secondary.observe_secondary(
        audio,
        media_path=media,
        provider="moss",
        duration_ms=1_000,
        supplement_source=source,
        crop_start_ms=10_000,
        crop_end_ms=11_000,
        exact_cue=True,
    )
    assert warm["served_from_cache"] is True
    warm_snapshot = budget.snapshot()
    assert warm_snapshot["attempt_count"] == 1
    assert warm_snapshot["total_audio_ms"] == 1_000
    assert warm_snapshot["cache_hit_count"] == 1
    assert warm_snapshot["cache_hits"][0]["provider"] == "moss"
    assert warm_snapshot["revision"] == 3


def test_provider_failure_seals_typed_reason_and_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    budget = start_budget(source)

    def fail(*_args: object, before_request, **_kwargs: object) -> None:
        before_request()
        raise DiarizedTranscriptionError(
            "MOSS_HTTP_ERROR",
            "sanitized",
            {"http_status": 429, "raw_response": "must not enter budget"},
        )

    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", fail)
    with pytest.raises(DiarizedTranscriptionError):
        secondary.observe_secondary(
            b"audio",
            media_path=tmp_path / "target.mp3",
            provider="moss",
            duration_ms=1_000,
            supplement_source=source,
            crop_start_ms=0,
            crop_end_ms=1_000,
            exact_cue=True,
        )

    snapshot = budget.snapshot()
    assert snapshot["attempt_count"] == 1
    assert snapshot["failed_attempt_count"] == 1
    assert snapshot["pending_attempt_count"] == 0
    assert snapshot["attempts"] == [
        {
            "attempt_id": 1,
            "provider": "moss",
            "model": MOSS_MODEL,
            "start_ms": 0,
            "end_ms": 1_000,
            "duration_ms": 1_000,
            "status": "FAILED",
            "reason_code": "MOSS_HTTP_ERROR",
            "http_status": 429,
        }
    ]
    assert "raw_response" not in json.dumps(snapshot)


def test_moss_unlocated_text_is_sealed_and_cacheable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    budget = start_budget(source)
    audio = b"audio"
    metadata = {
        **_evidence(audio, provider="moss"),
        "raw_response": {"text": "完整原话", "segments": []},
    }

    def fail(*_args: object, before_request, **_kwargs: object) -> None:
        before_request()
        raise DiarizedTranscriptionError(
            "MOSS_SEGMENT_INVALID",
            "bad range",
            metadata,
        )

    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", fail)
    first = secondary.observe_secondary(
        audio,
        media_path=tmp_path / "target.mp3",
        provider="moss",
        duration_ms=1_000,
        supplement_source=source,
        crop_start_ms=2_000,
        crop_end_ms=3_000,
        exact_cue=True,
    )
    assert first["status"] == "TEXT_UNLOCATED"
    assert first["served_from_cache"] is False
    assert budget.snapshot()["attempts"][0]["status"] == "TEXT_UNLOCATED"

    warm = secondary.observe_secondary(
        audio,
        media_path=tmp_path / "target.mp3",
        provider="moss",
        duration_ms=1_000,
        supplement_source=source,
        crop_start_ms=2_000,
        crop_end_ms=3_000,
        exact_cue=True,
    )
    assert warm["status"] == "TEXT_UNLOCATED"
    assert warm["served_from_cache"] is True
    snapshot = budget.snapshot()
    assert snapshot["attempt_count"] == 1
    assert snapshot["cache_hit_count"] == 1


def test_unlocated_response_binding_failure_seals_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    budget = start_budget(source)
    metadata = {
        **_evidence(b"different-audio", provider="moss"),
        "raw_response": {"text": "完整原话", "segments": []},
    }

    def fail(*_args: object, before_request, **_kwargs: object) -> None:
        before_request()
        raise DiarizedTranscriptionError(
            "MOSS_SEGMENT_INVALID",
            "bad range",
            metadata,
        )

    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", fail)
    with pytest.raises(
        ValueError,
        match="secondary response source binding mismatch",
    ):
        secondary.observe_secondary(
            b"audio",
            media_path=tmp_path / "target.mp3",
            provider="moss",
            duration_ms=1_000,
            supplement_source=source,
            crop_start_ms=3_000,
            crop_end_ms=4_000,
            exact_cue=True,
        )

    snapshot = budget.snapshot()
    assert snapshot["attempt_count"] == 1
    assert snapshot["pending_attempt_count"] == 0
    assert snapshot["attempts"][0]["status"] == "RESPONSE_REJECTED"
    assert (
        snapshot["attempts"][0]["reason_code"]
        == "SECONDARY_RESPONSE_SOURCE_BINDING_MISMATCH"
    )


def test_cap_refusal_is_disclosed_without_consuming_an_attempt(
    tmp_path: Path,
) -> None:
    budget = start_budget(
        tmp_path / "source.mp4",
        max_windows=1,
        max_audio_ms=2_000,
    )
    attempt_id = budget.consume("moss", MOSS_MODEL, 0, 1_000)
    budget.finish_attempt(attempt_id, status="OBSERVED")

    with pytest.raises(BudgetExceeded) as exc_info:
        budget.consume("mai", MAI_MODEL, 1_000, 2_000)

    assert exc_info.value.reason_code == WINDOW_CAP
    snapshot = budget.snapshot()
    assert snapshot["attempt_count"] == 1
    assert snapshot["total_audio_ms"] == 1_000
    assert snapshot["refusal_count"] == 1
    assert snapshot["refusals"][0]["reason_code"] == WINDOW_CAP
    assert snapshot["refusals"][0]["provider"] == "mai"
    assert snapshot["revision"] == 3

    revision = snapshot["revision"]
    budget.finish_attempt(attempt_id, status="OBSERVED")
    assert budget.snapshot()["revision"] == revision
    with pytest.raises(RuntimeError, match="already sealed"):
        budget.finish_attempt(
            attempt_id,
            status="FAILED",
            reason_code="LATE_REWRITE",
        )


def test_total_audio_refusal_counts_repeated_window_dispatches(
    tmp_path: Path,
) -> None:
    budget = start_budget(
        tmp_path / "source.mp4",
        max_windows=3,
        max_audio_ms=1_500,
    )
    attempt_id = budget.consume("moss", MOSS_MODEL, 0, 1_000)
    budget.finish_attempt(attempt_id, status="OBSERVED")

    with pytest.raises(BudgetExceeded) as exc_info:
        budget.consume("mai", MAI_MODEL, 0, 1_000)

    assert exc_info.value.reason_code == TOTAL_AUDIO_CAP
    snapshot = budget.snapshot()
    assert snapshot["distinct_window_count"] == 1
    assert snapshot["attempt_count"] == 1
    assert snapshot["total_audio_ms"] == 1_000
    assert snapshot["refusal_count"] == 1
    assert snapshot["refusals"][0]["reason_code"] == TOTAL_AUDIO_CAP
    assert snapshot["refusals"][0]["provider"] == "mai"
    assert snapshot["revision"] == 3


def test_receipt_is_self_bound_monotonic_and_rejects_future_revision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    budget = start_budget(source)
    attempt_id = budget.consume("moss", MOSS_MODEL, 0, 1_000)
    budget.finish_attempt(attempt_id, status="OBSERVED")
    receipt_path = tmp_path / "run" / "native-audio-budget.json"

    first = persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
    )
    assert first["schema_version"] == "native-audio-budget.v2"
    assert first["revision"] == 2
    assert first["receipt_sha256"] == receipt_sha256(first)

    budget.record_cache_hit(
        provider="moss",
        model=MOSS_MODEL,
        start_ms=0,
        end_ms=1_000,
    )
    second = persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
    )
    assert second["revision"] == 3
    assert second["receipt_sha256"] == receipt_sha256(second)
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == second

    future = deepcopy(second)
    future["revision"] = 4
    future_budget = future["budget"]
    assert isinstance(future_budget, dict)
    future_budget["revision"] = 4
    future["receipt_sha256"] = receipt_sha256(future)
    receipt_path.write_text(
        json.dumps(future, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="revision would regress",
    ):
        persist_native_audio_budget_receipt(
            source_media=source,
            source_media_sha256=source_sha,
            receipt_path=receipt_path,
        )


def test_native_consumers_share_one_canonical_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    canonical = tmp_path / "run" / "native-audio-budget.json"
    monkeypatch.setattr(
        native,
        "_extract_exact_mp3",
        lambda _source, output, _start, _end: output.write_bytes(b"audio"),
    )

    def observe(
        audio: bytes,
        *,
        provider: str,
        supplement_source: Path,
        crop_start_ms: int,
        crop_end_ms: int,
        **_kwargs: object,
    ) -> dict[str, object]:
        budget = get_budget(supplement_source)
        assert budget is not None
        model = {"moss": MOSS_MODEL, "mai": MAI_MODEL}[provider]
        attempt_id = budget.consume(
            provider,
            model,
            crop_start_ms,
            crop_end_ms,
        )
        budget.finish_attempt(attempt_id, status="OBSERVED")
        return _evidence(audio, provider=provider)

    monkeypatch.setattr(native, "observe_secondary", observe)
    moss = native.build_native_foreign_witness(
        source_media=source,
        output_dir=tmp_path / "context",
        provider="moss",
        max_windows=4,
        max_audio_ms=4_000,
        budget_receipt_path=canonical,
    )
    moss(start_ms=0, end_ms=1_000)
    first = json.loads(canonical.read_text(encoding="utf-8"))
    assert first["revision"] == 2
    assert first["providers"] == ["moss"]

    mai = native.build_native_foreign_witness(
        source_media=source,
        output_dir=tmp_path / "foreign",
        provider="mai",
        budget_receipt_path=canonical,
    )
    mai(start_ms=1_000, end_ms=2_000)
    second = json.loads(canonical.read_text(encoding="utf-8"))
    assert second["revision"] == 4
    assert second["providers"] == ["mai", "moss"]
    assert second["budget"]["attempt_count"] == 2
    assert second["receipt_sha256"] == receipt_sha256(second)
    assert not (tmp_path / "context" / "native-audio-budget.json").exists()
    assert not (tmp_path / "foreign" / "native-audio-budget.json").exists()
