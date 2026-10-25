from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.autoslice import native_audio_budget_receipt as receipts
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
    resume_native_audio_budget,
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


def test_long_semantic_range_preserves_native_receipt_continuity(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    path = tmp_path / "native-audio-budget.json"
    budget = start_budget(source)
    attempt = budget.consume("moss", MOSS_MODEL, 65_480, 96_750)
    budget.finish_attempt(attempt, status="OBSERVED")
    budget.record_cache_hit(provider="moss", model=MOSS_MODEL, start_ms=65_480, end_ms=96_750)
    persist_native_audio_budget_receipt(
        source_media=source, source_media_sha256=source_sha, receipt_path=path,
    )
    frozen = path.read_bytes()
    expected = budget.snapshot()
    resumed = start_budget(source)
    resume_native_audio_budget(
        source_media=source, source_media_sha256=source_sha, receipt_path=path,
        require_existing=True,
    )
    assert resumed.snapshot() == expected
    assert resumed.snapshot()["total_audio_ms"] == 31_270
    assert path.read_bytes() == frozen


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


def test_explicit_cap_refusal_is_disclosed_without_consuming_an_attempt(
    tmp_path: Path,
) -> None:
    budget = start_budget(
        tmp_path / "source.mp4",
        max_windows=1,
        max_audio_ms=2_000,
        enforce_limits=True,
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


def test_explicit_total_audio_refusal_counts_repeated_window_dispatches(
    tmp_path: Path,
) -> None:
    budget = start_budget(
        tmp_path / "source.mp4",
        max_windows=3,
        max_audio_ms=1_500,
        enforce_limits=True,
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


def test_soft_pressure_overage_persists_and_resumes_without_provider(
    tmp_path: Path,
) -> None:
    source = tmp_path / "soft-overage.mp4"
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt_path = tmp_path / "run" / "native-audio-budget.json"
    budget = start_budget(source, max_windows=3, max_audio_ms=60_000)

    for index in range(4):
        start_ms = index * 20_000
        attempt_id = budget.consume("moss", MOSS_MODEL, start_ms, start_ms + 20_000)
        budget.finish_attempt(attempt_id, status="OBSERVED")

    first = persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
    )
    assert first["budget"]["enforce_limits"] is False
    assert first["budget"]["distinct_window_count"] == 4
    assert first["budget"]["total_audio_ms"] == 80_000
    assert first["budget"]["attempt_count"] == 4
    assert first["budget"]["refusals"] == []

    # Simulate a v2 receipt written before the mode field existed.  The
    # overage and every event row must remain intact through restart and the
    # compatibility upgrade on the next persist.
    legacy = deepcopy(first)
    legacy_budget = legacy["budget"]
    assert isinstance(legacy_budget, dict)
    legacy_budget.pop("enforce_limits")
    legacy["receipt_sha256"] = receipt_sha256(legacy)
    receipt_path.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restarted = start_budget(source, max_windows=3, max_audio_ms=60_000)
    resume_native_audio_budget(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
        require_existing=True,
    )
    assert get_budget(source) is restarted
    restored = restarted.snapshot()
    assert restored["enforce_limits"] is False
    assert restored["attempts"] == legacy_budget["attempts"]
    assert restored["refusals"] == legacy_budget["refusals"]
    assert restored["distinct_window_count"] == 4
    assert restored["total_audio_ms"] == 80_000

    upgraded = persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
    )
    assert upgraded["budget"]["enforce_limits"] is False
    assert upgraded["budget"]["attempts"] == legacy_budget["attempts"]
    assert upgraded["budget"]["refusals"] == legacy_budget["refusals"]
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == upgraded


def test_native_consumer_persists_and_resumes_soft_overage_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "native-soft-overage.mp4"
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt_path = tmp_path / "run" / "native-audio-budget.json"

    monkeypatch.setattr(
        native,
        "_extract_exact_mp3",
        lambda _source, output, start_ms, _end_ms: output.write_bytes(
            f"audio-{start_ms}".encode()
        ),
    )
    monkeypatch.setattr(
        native,
        "exact_target_evidence",
        lambda *_args, **_kwargs: {"native_segments": []},
    )

    def dispatch(audio: bytes, **kwargs: object) -> dict[str, object]:
        before_request = kwargs["before_request"]
        assert callable(before_request)
        before_request()
        return {
            "provider": kwargs["provider"],
            "model": MOSS_MODEL,
            "status": "OBSERVED",
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "a" * 64,
            "native_segments": [],
            "native_timeline": {"valid": True},
            "raw_response": {"text": "synthetic"},
        }

    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", dispatch)
    observer = native.build_native_foreign_witness(
        source_media=source,
        output_dir=tmp_path / "evidence",
        provider="moss",
        max_windows=3,
        max_audio_ms=60_000,
        budget_receipt_path=receipt_path,
    )
    for index in range(4):
        start_ms = index * 20_000
        result = observer(start_ms=start_ms, end_ms=start_ms + 20_000)
        assert result["served_from_cache"] is False

    packet = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert packet["budget"]["enforce_limits"] is False
    assert packet["budget"]["attempt_count"] == 4
    assert packet["budget"]["distinct_window_count"] == 4
    assert packet["budget"]["total_audio_ms"] == 80_000
    assert packet["budget"]["refusals"] == []

    restarted = start_budget(source, max_windows=3, max_audio_ms=60_000)
    resume_native_audio_budget(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
        require_existing=True,
    )
    assert restarted.snapshot()["attempt_count"] == 4
    assert restarted.snapshot()["distinct_window_count"] == 4
    assert restarted.snapshot()["total_audio_ms"] == 80_000
    assert restarted.snapshot()["enforce_limits"] is False


def test_explicit_hard_mode_is_continuous_and_cannot_resume_soft(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hard-mode.mp4"
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt_path = tmp_path / "run" / "native-audio-budget.json"
    hard = start_budget(
        source,
        max_windows=1,
        max_audio_ms=20_000,
        enforce_limits=True,
    )
    attempt_id = hard.consume("moss", MOSS_MODEL, 0, 20_000)
    hard.finish_attempt(attempt_id, status="OBSERVED")
    persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
    )

    soft = start_budget(source, max_windows=1, max_audio_ms=20_000)
    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="enforcement mode changed",
    ):
        resume_native_audio_budget(
            source_media=source,
            source_media_sha256=source_sha,
            receipt_path=receipt_path,
            require_existing=True,
        )
    assert soft.snapshot()["attempt_count"] == 0

    resumed_hard = start_budget(
        source,
        max_windows=1,
        max_audio_ms=20_000,
        enforce_limits=True,
    )
    resume_native_audio_budget(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt_path,
        require_existing=True,
    )
    assert resumed_hard.snapshot()["enforce_limits"] is True
    with pytest.raises(BudgetExceeded) as exc_info:
        resumed_hard.consume("moss", MOSS_MODEL, 20_000, 40_000)
    assert exc_info.value.reason_code == WINDOW_CAP


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


def _relocation_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str, bytes, dict, object]:
    original = tmp_path / "original-source.mp4"
    relocated = tmp_path / "relocated-source.mp4"
    original.write_bytes(b"byte-identical source")
    relocated.write_bytes(original.read_bytes())
    receipt = tmp_path / "run" / "native-audio-budget.json"
    budget = start_budget(original, max_windows=4, max_audio_ms=4_000)
    attempt = budget.consume("moss", MOSS_MODEL, 0, 1_000)
    budget.finish_attempt(attempt, status="OBSERVED")
    budget.record_cache_hit(
        provider="moss", model=MOSS_MODEL, start_ms=0, end_ms=1_000,
    )
    source_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    first = persist_native_audio_budget_receipt(
        source_media=original,
        source_media_sha256=source_sha,
        receipt_path=receipt,
    )
    original_raw = receipt.read_bytes()
    restarted = start_budget(relocated, max_windows=4, max_audio_ms=4_000)
    return original, relocated, receipt, source_sha, original_raw, first, restarted


def test_same_bytes_relocation_preserves_archive_and_allows_append(
    tmp_path: Path,
) -> None:
    original, relocated, receipt, source_sha, original_raw, first, restarted = (
        _relocation_fixture(tmp_path)
    )
    resume_native_audio_budget(
        source_media=relocated,
        source_media_sha256=source_sha,
        receipt_path=receipt,
        require_existing=True,
    )

    packet = json.loads(receipt.read_bytes())
    expected = deepcopy(first)
    expected_budget = expected["budget"]
    assert isinstance(expected_budget, dict)
    expected_budget["source_media"] = str(relocated.resolve())
    expected["receipt_sha256"] = receipt_sha256(expected)
    assert packet == expected
    archive = receipts._relocation_archive_path(receipt, first["receipt_sha256"])
    assert archive.read_bytes() == original_raw
    assert packet["budget"]["attempts"] == first["budget"]["attempts"]
    assert packet["budget"]["cache_hits"] == first["budget"]["cache_hits"]
    assert packet["budget"]["revision"] == first["budget"]["revision"]

    next_attempt = restarted.consume("moss", MOSS_MODEL, 2_000, 3_000)
    restarted.finish_attempt(next_attempt, status="OBSERVED")
    appended = persist_native_audio_budget_receipt(
        source_media=relocated,
        source_media_sha256=source_sha,
        receipt_path=receipt,
    )
    assert appended["budget"]["attempt_count"] == 2
    assert appended["budget"]["cache_hit_count"] == 1
    assert appended["budget"]["source_media"] == str(relocated.resolve())
    assert archive.read_bytes() == original_raw
    assert original.exists()


def test_same_target_resume_is_idempotent_without_archive_or_rewrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same target")
    receipt = tmp_path / "run" / "native-audio-budget.json"
    budget = start_budget(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    first = persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt,
    )
    before = receipt.read_bytes()

    resume_native_audio_budget(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=receipt,
        require_existing=True,
    )

    assert receipt.read_bytes() == before
    assert json.loads(before) == first
    assert not list(receipt.parent.glob(f"{receipt.name}.original-*"))
    assert budget.snapshot()["revision"] == 0


@pytest.mark.parametrize("drift", ["replacement", "original"])
def test_relocation_rejects_different_bytes_without_receipt_or_history_change(
    tmp_path: Path, drift: str,
) -> None:
    original, relocated, receipt, source_sha, original_raw, _first, restarted = (
        _relocation_fixture(tmp_path)
    )
    if drift == "replacement":
        relocated.write_bytes(b"different replacement bytes")
    else:
        original.write_bytes(b"original source drift")
    before = receipt.read_bytes()
    before_snapshot = restarted.snapshot()

    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="source bytes do not match frozen SHA",
    ):
        resume_native_audio_budget(
            source_media=relocated,
            source_media_sha256=source_sha,
            receipt_path=receipt,
            require_existing=True,
        )

    assert receipt.read_bytes() == before
    assert restarted.snapshot() == before_snapshot
    assert not list(receipt.parent.glob(f"{receipt.name}.original-*"))
    assert original_raw == before


def test_source_hash_rejects_in_place_identity_drift_while_fd_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable before read")
    real_fstat = os.fstat
    calls = 0

    def mutate_after_open(fd: int) -> os.stat_result:
        nonlocal calls
        result = real_fstat(fd)
        calls += 1
        if calls == 1:
            with source.open("ab") as handle:
                handle.write(b"drift")
        return result

    monkeypatch.setattr(receipts.os, "fstat", mutate_after_open)
    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="source changed while hashing",
    ):
        receipts._source_sha256_stable(source, label="test")
    assert calls == 2


def test_relocation_rejects_source_symlink_without_receipt_or_history_change(
    tmp_path: Path,
) -> None:
    original, relocated, receipt, source_sha, _raw, _first, restarted = (
        _relocation_fixture(tmp_path)
    )
    link = tmp_path / "relocated-link.mp4"
    link.symlink_to(relocated)
    before = receipt.read_bytes()
    before_snapshot = restarted.snapshot()

    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="replacement source locator is unavailable",
    ):
        resume_native_audio_budget(
            source_media=link,
            source_media_sha256=source_sha,
            receipt_path=receipt,
            require_existing=True,
        )

    assert receipt.read_bytes() == before
    assert restarted.snapshot() == before_snapshot
    assert original.read_bytes() == relocated.read_bytes()
    assert not list(receipt.parent.glob(f"{receipt.name}.original-*"))


def test_relocation_rejects_original_source_symlink_without_mutation(
    tmp_path: Path,
) -> None:
    original, relocated, receipt, source_sha, _raw, _first, restarted = (
        _relocation_fixture(tmp_path)
    )
    original.unlink()
    original.symlink_to(tmp_path / "missing-original-target.mp4")
    before = receipt.read_bytes()
    before_snapshot = restarted.snapshot()

    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="original source locator is unavailable",
    ):
        resume_native_audio_budget(
            source_media=relocated,
            source_media_sha256=source_sha,
            receipt_path=receipt,
            require_existing=True,
        )

    assert receipt.read_bytes() == before
    assert restarted.snapshot() == before_snapshot
    assert not list(receipt.parent.glob(f"{receipt.name}.original-*"))


def test_relocation_preserves_pending_unknown_attempt_without_refund(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original-source.mp4"
    relocated = tmp_path / "relocated-source.mp4"
    original.write_bytes(b"pending source")
    relocated.write_bytes(original.read_bytes())
    receipt = tmp_path / "run" / "native-audio-budget.json"
    budget = start_budget(original, max_windows=2, max_audio_ms=2_000)
    attempt = budget.consume("moss", MOSS_MODEL, 0, 1_000)
    source_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    first = persist_native_audio_budget_receipt(
        source_media=original,
        source_media_sha256=source_sha,
        receipt_path=receipt,
    )
    restarted = start_budget(relocated, max_windows=2, max_audio_ms=2_000)

    resume_native_audio_budget(
        source_media=relocated,
        source_media_sha256=source_sha,
        receipt_path=receipt,
        require_existing=True,
    )

    snapshot = restarted.snapshot()
    assert snapshot["attempts"] == first["budget"]["attempts"]
    assert snapshot["attempts"][0]["status"] == "DISPATCHED"
    assert snapshot["pending_attempt_count"] == 1
    assert snapshot["total_audio_ms"] == 1_000
    assert json.loads(receipt.read_bytes())["budget"] == snapshot
    assert json.loads(receipt.read_bytes())["budget"]["attempts"][0]["attempt_id"] == attempt


def test_conflicting_target_history_blocks_before_relocation_archive(
    tmp_path: Path,
) -> None:
    _original, relocated, receipt, source_sha, before, _first, restarted = (
        _relocation_fixture(tmp_path)
    )
    conflict = restarted.consume("moss", MOSS_MODEL, 3_000, 4_000)
    restarted.finish_attempt(conflict, status="OBSERVED")
    before_snapshot = restarted.snapshot()

    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="prior attempt identity changed|history conflicts",
    ):
        resume_native_audio_budget(
            source_media=relocated,
            source_media_sha256=source_sha,
            receipt_path=receipt,
            require_existing=True,
        )

    assert receipt.read_bytes() == before
    assert restarted.snapshot() == before_snapshot
    assert not list(receipt.parent.glob(f"{receipt.name}.original-*"))


def test_occupied_relocation_archive_must_match_original_raw_receipt(
    tmp_path: Path,
) -> None:
    original, relocated, receipt, source_sha, original_raw, first, _restarted = (
        _relocation_fixture(tmp_path)
    )
    first_target = start_budget(relocated, max_windows=4, max_audio_ms=4_000)
    resume_native_audio_budget(
        source_media=relocated,
        source_media_sha256=source_sha,
        receipt_path=receipt,
        require_existing=True,
    )
    current_raw = receipt.read_bytes()
    next_source = tmp_path / "next-source.mp4"
    next_source.write_bytes(original.read_bytes())
    next_budget = start_budget(next_source, max_windows=4, max_audio_ms=4_000)
    archive = receipts._relocation_archive_path(
        receipt, json.loads(current_raw)["receipt_sha256"]
    )
    archive.write_bytes(b"occupied archive with wrong bytes")
    before_snapshot = next_budget.snapshot()

    with pytest.raises(
        BudgetReceiptPersistenceError,
        match="relocation archive conflicts",
    ):
        resume_native_audio_budget(
            source_media=next_source,
            source_media_sha256=source_sha,
            receipt_path=receipt,
            require_existing=True,
        )

    assert receipt.read_bytes() == current_raw
    assert next_budget.snapshot() == before_snapshot
    assert archive.read_bytes() != original_raw
    assert first_target.snapshot()["revision"] == first["budget"]["revision"]
