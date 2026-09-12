"""Exercise native ASR through the real mixed-script CPA consumer."""

from __future__ import annotations

import hashlib
import json

import pytest

from src.autoslice import foreign_span_witness as fsw
from src.autoslice.subtitle_fidelity import audit_foreign_script_consistency


def srt(text: str) -> str:
    return "1\n00:00:02,000 --> 00:00:04,500\n" + text + "\n"


def test_native_route_keeps_cpa_authority_and_uses_exact_target(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native

    current = srt("you们知道苹果要出，呃，you")
    transcript = "你们知道苹果要出，呃，you"
    calls = []

    def factory(**kwargs):
        assert kwargs["provider"] == "mai"

        def observe(*, start_ms, end_ms):
            calls.append((start_ms, end_ms))
            return {
                "schema_version": "exact-target-native-asr-evidence.v1",
                "status": "OBSERVED",
                "transcript": transcript,
                "provider": "mai",
                "model": "MAI-Transcribe-2",
                "input_audio_sha256": "a" * 64,
                "source_media_sha256": "b" * 64,
                "response_sha256": "c" * 64,
                "target_start_ms": start_ms,
                "target_end_ms": end_ms,
                "candidate_exposure": "none",
                "mutation_authorized": False,
            }

        return observe

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    monkeypatch.setattr(fsw, "_observe_audio", lambda **_: pytest.fail("AGY must not run first"))
    monkeypatch.setattr(
        fsw, "_extract_span_audio", lambda *_: pytest.fail("padded legacy crop must not be used")
    )
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        assert transcript in prompt and "you们知道苹果要出" in prompt
        assert current in prompt
        return json.dumps(
            {
                "choice": "PROPOSED",
                "candidate_id": "PROPOSAL",
                "reason": "Synthetic test verdict; not an audio assessment.",
            }
        )

    output, audit = fsw.adjudicate_foreign_script_audit(
        media_path=tmp_path / "media.mp4",
        srt_text=current,
        audit=audit_foreign_script_consistency(current),
        out_root=tmp_path,
        cid="fixture",
        llm_call=judge,
        native_provider="mai",
    )
    assert output == srt(transcript), json.dumps(audit, ensure_ascii=False)
    assert calls == [(2000, 4500)]
    assert len(prompts) == 1  # Similarity cannot waive CPA for this route.
    assert audit["decision_authority"] == "CPA_JUDGE"
    assert audit["audio_witness_rows"][0]["model"] == "MAI-Transcribe-2"
    assert audit["audio_witness_rows"][0]["witnessed"] is False
    assert "heard_pinyin" not in json.dumps(audit["audio_witness_rows"])


def test_native_failure_does_not_silently_invoke_another_provider(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native

    def factory(**kwargs):
        def observe(**_):
            raise RuntimeError("NATIVE_TEST_UNAVAILABLE")

        return observe

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    monkeypatch.setattr(fsw, "_observe_audio", lambda **_: pytest.fail("unrequested fallback"))
    text = srt("you们知道苹果要出，呃，you")
    output, audit = fsw.adjudicate_foreign_script_audit(
        media_path=tmp_path / "media.mp4",
        srt_text=text,
        audit=audit_foreign_script_consistency(text),
        out_root=tmp_path,
        cid="fixture",
        llm_call=lambda _: pytest.fail("no fabricated witness"),
        native_provider="moss",
    )
    assert output == text
    assert audit["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    assert "NATIVE_TEST_UNAVAILABLE" in audit["audio_witness_rows"][0]["failure"]


def test_unknown_native_provider_fails_before_audio(tmp_path):
    text = srt("you们知道苹果要出，呃，you")
    with pytest.raises(ValueError):
        fsw.adjudicate_foreign_script_audit(
            media_path=tmp_path / "absent.mp4",
            srt_text=text,
            audit=audit_foreign_script_consistency(text),
            out_root=tmp_path,
            cid="fixture",
            llm_call=None,
            native_provider="auto",
        )


def test_target_clip_assembler_refuses_partial_source_and_keeps_no_candidate_input(
    tmp_path, monkeypatch
):
    from src.autoslice import native_foreign_witness as native

    source = tmp_path / "source.wav"
    source.write_bytes(b"fixture-media")
    monkeypatch.setattr(
        native,
        "_extract_exact_mp3",
        lambda source, output, start_ms, end_ms: output.write_bytes(b"audio"),
    )
    requests = []

    def transcribe(audio, **kwargs):
        requests.append((audio, kwargs))
        return {
            "provider": "mai",
            "model": "MAI-Transcribe-2",
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "c" * 64,
            "native_segments": [{"start_ms": 0, "end_ms": 500, "text": "原话"}],
        }

    monkeypatch.setattr(native, "observe_secondary", transcribe)
    observe = native.build_native_foreign_witness(
        source_media=source, output_dir=tmp_path / "out", provider="mai"
    )
    result = observe(start_ms=1000, end_ms=2000)
    assert result["target_start_ms"] == 1000 and result["target_end_ms"] == 2000
    assert result["candidate_exposure"] == "none"
    assert result["mutation_authorized"] is False
    assert not any(
        k in requests[0][1] for k in ("prompt", "current", "candidate", "replacement", "context")
    )
    source.write_bytes(b"changed-source")
    with pytest.raises(ValueError, match="source"):
        observe(start_ms=1000, end_ms=2000)
    assert len(requests) == 1


def test_native_source_symlink_rejected_before_resolve(tmp_path):
    from src.autoslice.native_foreign_witness import build_native_foreign_witness

    source = tmp_path / "source.wav"
    source.write_bytes(b"x")
    link = tmp_path / "link.wav"
    link.symlink_to(source)
    with pytest.raises(ValueError):
        build_native_foreign_witness(source_media=link, output_dir=tmp_path / "out", provider="mai")


def test_actual_producer_stage_registers_selected_native_text(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native
    from src.autoslice import producer_text_pipeline as pipeline

    text = srt("you们知道苹果要出，呃，you")
    after = "你们知道苹果要出，呃，you"

    def factory(**_):
        return lambda **_: {
            "status": "OBSERVED",
            "transcript": after,
            "provider": "moss",
            "model": "moss-transcribe-diarize-pro",
            "input_audio_sha256": "a" * 64,
            "source_media_sha256": "b" * 64,
            "response_sha256": "c" * 64,
            "candidate_exposure": "none",
            "mutation_authorized": False,
        }

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (
            lambda _: json.dumps(
                {
                    "choice": "PROPOSED",
                    "candidate_id": "PROPOSAL",
                    "reason": "Synthetic regression choice.",
                }
            )
        ),
    )
    ledger = {}
    output, audit = pipeline._adjudicate_final_language(
        tmp_path / "media.mp4",
        text,
        audit_foreign_script_consistency(text),
        tmp_path,
        "fixture",
        ledger,
        source_language=False,
        source_srt=text,
        native_provider="moss",
    )
    assert output == srt(after)
    assert audit["applied_count"] == 1
    owner = ledger["entity_repairs"][0]
    assert owner["decision_authority"] == "CPA_JUDGE"
    assert owner["mutation_authority"]["status"] == "PASS"
    assert owner["matched_start_ms"] == 2000 and owner["matched_end_ms"] == 4500
    assert owner["after"] == [after]


def test_native_cache_hit_does_not_consume_another_provider_attempt(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native
    from src.autoslice import diarized_transcription
    from src.autoslice.supplement_audio_budget import get_budget

    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        native,
        "_extract_exact_mp3",
        lambda source, output, start_ms, end_ms: output.write_bytes(b"audio"),
    )
    calls = []

    def provider(audio, **kwargs):
        calls.append(kwargs["provider"])
        kwargs["before_request"]()
        return {
            "provider": "mai",
            "model": "MAI-Transcribe-2",
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "c" * 64,
            "native_segments": [{"start_ms": 0, "end_ms": 500, "text": "原话"}],
        }

    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", provider)
    observe = native.build_native_foreign_witness(
        source_media=source, output_dir=tmp_path / "out", provider="mai"
    )
    first = observe(start_ms=1000, end_ms=2000)
    second = observe(start_ms=1000, end_ms=2000)
    assert first["served_from_cache"] is False and second["served_from_cache"] is True
    assert calls == ["mai"]
    assert get_budget(source).snapshot()["attempt_count"] == 1
    assert get_budget(source).snapshot()["total_audio_ms"] == 1000


def test_exact_crop_no_padding_and_truncated_source_refused(tmp_path):
    import math
    import shutil
    import struct
    import subprocess
    import wave
    from src.autoslice.native_foreign_witness import _extract_exact_mp3

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable; no media validation claim")
    source = tmp_path / "source.wav"
    # Tone outside the target, silence inside it: a padded crop leaks the tone.
    values = [
        int(12000 * math.sin(2 * math.pi * 440 * n / 16000)) if n < 16000 else 0
        for n in range(48000)
    ]
    with wave.open(str(source), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(struct.pack("<" + "h" * len(values), *values))
    output = tmp_path / "target.mp3"
    _extract_exact_mp3(source, output, 1000, 2000)
    pcm = subprocess.check_output(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(output),
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        timeout=20,
    )
    assert len(pcm) == 32000
    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    assert max(abs(x) for x in samples) == 0
    with pytest.raises(RuntimeError, match="decoded target duration mismatch"):
        _extract_exact_mp3(source, tmp_path / "truncated.mp3", 2500, 4000)
    assert not (tmp_path / "truncated.mp3").exists()


def test_native_overlap_cannot_be_flattened_into_an_exact_claim(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native

    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        native,
        "_extract_exact_mp3",
        lambda source, output, start_ms, end_ms: output.write_bytes(b"audio"),
    )
    monkeypatch.setattr(
        native,
        "observe_secondary",
        lambda audio, **_: {
            "provider": "moss",
            "model": "moss-transcribe-diarize-pro",
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "c" * 64,
            "native_segments": [
                {"start_ms": 0, "end_ms": 700, "text": "甲"},
                {"start_ms": 500, "end_ms": 800, "text": "乙"},
            ],
        },
    )
    observe = native.build_native_foreign_witness(
        source_media=source, output_dir=tmp_path / "out", provider="moss"
    )
    with pytest.raises(ValueError, match="overlap"):
        observe(start_ms=1000, end_ms=2000)


def test_clean_cue_does_not_invoke_native_audio(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native

    monkeypatch.setattr(
        native,
        "build_native_foreign_witness",
        lambda **_: pytest.fail("clean text needs no new witness"),
    )
    text = srt("好好好好好啊哈")
    output, audit = fsw.adjudicate_foreign_script_audit(
        media_path=tmp_path / "source",
        srt_text=text,
        audit=audit_foreign_script_consistency(text),
        out_root=tmp_path,
        cid="fixture",
        llm_call=lambda _: pytest.fail("clean text needs no new judge"),
        native_provider="moss",
    )
    assert output == text and audit["status"] == "CLEAN"


def test_native_typed_failure_is_retained_for_recovery(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native
    from src.autoslice.diarized_transcription import DiarizedTranscriptionError

    def factory(**_):
        def observer(**_):
            raise DiarizedTranscriptionError("MAI_HTTP_ERROR", "sanitized", {"http_status": 429})

        return observer

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    text = srt("you们知道苹果要出，呃，you")
    _, audit = fsw.adjudicate_foreign_script_audit(
        media_path=tmp_path / "source",
        srt_text=text,
        audit=audit_foreign_script_consistency(text),
        out_root=tmp_path,
        cid="fixture",
        llm_call=None,
        native_provider="mai",
    )
    row = audit["audio_witness_rows"][0]
    assert row["provider"] == "mai"
    assert row["failure_reason_code"] == "MAI_HTTP_ERROR"
    assert row["failure_http_status"] == 429
