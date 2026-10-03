from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.free_asr_client import to_srt
from src.autoslice import final_subtitle_audio_gate as gate
from src.autoslice import producer_delivery_prepare as delivery_prepare
from src.autoslice import producer_package_finalization as finalization


def _time(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _srt(rows: list[tuple[int, int, str]]) -> str:
    return "\n\n".join(
        f"{index}\n{_time(start)} --> {_time(end)}\n{text}"
        for index, (start, end, text) in enumerate(rows, start=1)
    ) + "\n"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


_ROWS = [
    (1_000, 5_000, "开场这句内容很特别"),
    (23_000, 27_000, "中段这里继续说明问题"),
    (42_000, 46_000, "最后我们明确完成收束"),
]


def _raw_result(rows: list[tuple[int, int, str]]) -> dict:
    return {
        "utterances": [
            {
                "start_time": start,
                "end_time": end,
                "transcript": text,
                "words": [],
            }
            for start, end, text in rows
        ]
    }


def _record(
    tmp_path: Path,
    *,
    intro_offset_ms: int = 6_200,
    rows: list[tuple[int, int, str]] = _ROWS,
) -> tuple[dict, Path, Path]:
    media = tmp_path / "candidate.burned.mp4"
    media.write_bytes(b"exact burned media")
    digest = _sha(media)
    record = {
        "artifact_hashes": {"burned_video_sha256": digest},
        "burned_preview": {
            "status": "BURNED",
            "path": str(media),
            "burned_sha256": digest,
            "branding_intro": {"intro_offset_ms": intro_offset_ms},
        },
    }
    final_srt = tmp_path / "candidate.recut.srt"
    final_srt.write_text(_srt(rows), encoding="utf-8")
    return record, final_srt, media


def _adapters(
    *,
    witness_rows: list[tuple[int, int, str]],
    calls: dict[str, int],
) -> gate.FinalSubtitleAudioGateAdapters:
    raw = _raw_result(witness_rows)

    def extract(_media: Path, output: Path) -> None:
        calls["extract"] = calls.get("extract", 0) + 1
        output.write_bytes(b"normalized extracted audio")

    def transcribe(sound: bytes) -> dict:
        assert sound == b"normalized extracted audio"
        calls["transcribe"] = calls.get("transcribe", 0) + 1
        return raw

    def render(result: dict) -> str:
        calls["to_srt"] = calls.get("to_srt", 0) + 1
        return to_srt(result)

    return gate.FinalSubtitleAudioGateAdapters(extract, transcribe, render)


def test_capture_binds_actual_burn_and_reuses_exact_cache(tmp_path: Path) -> None:
    record, final_srt, _media = _record(tmp_path)
    witness_rows = [
        (start + 6_200, end + 6_200, text) for start, end, text in _ROWS
    ]
    calls: dict[str, int] = {}
    adapters = _adapters(witness_rows=witness_rows, calls=calls)

    captured = gate.capture_final_subtitle_audio_check(
        record, final_srt, tmp_path / "evidence", "candidate", adapters=adapters
    )

    evidence = captured["subtitle_audio_correspondence"]
    assert evidence["status"] == "PASS"
    assert evidence["timing_status"] == "PASS"
    assert evidence["intro_offset_ms"] == 6_200
    assert evidence["provider"] == "bcut"
    assert calls == {"extract": 1, "transcribe": 1, "to_srt": 1}
    for path in gate.subtitle_audio_artifact_paths(captured):
        assert path.is_file()

    def unavailable(*_args):
        raise AssertionError("exact same-media cache must avoid a provider call")

    reused = gate.capture_final_subtitle_audio_check(
        captured,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=gate.FinalSubtitleAudioGateAdapters(unavailable, unavailable, unavailable),
    )
    assert reused["subtitle_audio_correspondence"]["cache_reused"] is True
    assert calls == {"extract": 1, "transcribe": 1, "to_srt": 1}


def test_wrong_time_witness_is_a_blocking_capture_result(tmp_path: Path) -> None:
    record, final_srt, _media = _record(tmp_path, intro_offset_ms=6_200)
    wrong_rows = [
        (start + 9_750, end + 9_750, text) for start, end, text in _ROWS
    ]
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "wrong-evidence",
        "candidate",
        adapters=_adapters(witness_rows=wrong_rows, calls={}),
    )

    evidence = captured["subtitle_audio_correspondence"]
    assert evidence["status"] == "BLOCK"
    assert evidence["timing_status"] == "BLOCK"
    assert "GLOBAL_TIMING_SHIFT" in evidence["reason_codes"]


def test_producer_refuses_to_return_before_stage_when_gate_blocks(
    tmp_path: Path,
) -> None:
    record, final_srt, burned = _record(tmp_path, intro_offset_ms=0)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    calls: dict[str, int] = {}
    wrong_rows = [(start + 9_750, end + 9_750, text) for start, end, text in _ROWS]
    gate_adapters = _adapters(witness_rows=wrong_rows, calls=calls)

    def capture(record_value, final_path, output_dir, candidate_id):
        return gate.capture_final_subtitle_audio_check(
            record_value,
            final_path,
            output_dir,
            candidate_id,
            adapters=gate_adapters,
        )

    def burn(record_value, **_kwargs):
        result = dict(record_value)
        result["burned_preview"] = {
            "status": "BURNED",
            "path": str(burned),
            "burned_sha256": _sha(burned),
            "branding_intro": None,
        }
        result.setdefault("artifact_hashes", {})["burned_video_sha256"] = _sha(burned)
        return result

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated producer adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=burn,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        capture_subtitle_audio_check=capture,
    )
    options = finalization.ProducerFinalizationOptions(
        spec=tmp_path / "spec.json",
        substrate="source",
        correct="reviewed",
        speaker_mode="off",
        speaker_overrides=None,
        speaker_source_session_anchors=None,
        speaker_mixed_overlap_evidence=None,
        speaker_python=tmp_path / "python",
        reuse_cover=True,
    )

    with pytest.raises(SystemExit, match="FINAL_SUBTITLE_AUDIO_CHECK_BLOCKED"):
        finalization._build_and_burn_record(
            options=options,
            profile_id="lidousha",
            speaker_subtitle_style_id="style",
            final_start=0,
            final_end=46_000,
            recut=finalization.FinalRecutArtifacts(
                recut_dir=tmp_path,
                media_path=burned,
                subtitle_path=final_srt,
                text_manifest_path=None,
                text_manifest=None,
            ),
            speaker=finalization.SpeakerArtifacts(None, None, None, None),
            authority=finalization.AuthorityArtifacts(None, None),
            chat_authority_audit={},
            chat_authority_path=chat_path,
            timing_qa={},
            audit={},
            branding_intro=None,
            adapters=adapters,
            candidate_id="candidate",
        )
    assert not (tmp_path / "candidate.record.json").exists()


def test_validation_rejects_media_and_witness_drift(tmp_path: Path) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    assert gate.validate_final_subtitle_audio_check(captured, final_srt, media)["status"] == "PASS"

    other_media = tmp_path / "other.mp4"
    other_media.write_bytes(b"different media")
    with pytest.raises(ValueError, match="MEDIA_HASH_DRIFT"):
        gate.validate_final_subtitle_audio_check(captured, final_srt, other_media)

    witness = gate.subtitle_audio_artifact_paths(captured)[0]
    witness.write_text(witness.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="WITNESS_HASH_DRIFT"):
        gate.validate_final_subtitle_audio_check(captured, final_srt, media)


def test_validation_requires_canonical_package_stem_and_supports_renames(
    tmp_path: Path,
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    package = tmp_path / "package"
    package.mkdir()
    with pytest.raises(ValueError, match="PACKAGE_EVIDENCE_MISSING"):
        gate.validate_final_subtitle_audio_check(
            captured, final_srt, media, package_root=package
        )

    renamed = package / "发布标题.srt"
    renamed.write_bytes(final_srt.read_bytes())
    for source in gate.subtitle_audio_artifact_paths(captured):
        suffix = source.name.removeprefix("candidate")
        (package / f"发布标题{suffix}").write_bytes(source.read_bytes())
    receipt = gate.validate_final_subtitle_audio_check(
        captured, renamed, media, package_root=package
    )
    assert receipt["status"] == "PASS"


def test_validation_rederives_witness_from_raw_result(tmp_path: Path) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    paths = gate.subtitle_audio_artifact_paths(captured)
    raw_path = paths[3]
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw["utterances"][0]["transcript"] = "篡改后的原始识别"
    raw_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    envelope_path = paths[2]
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["raw_result_sha256"] = _sha(raw_path)
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    captured["subtitle_audio_correspondence"]["raw_result_sha256"] = _sha(raw_path)

    with pytest.raises(ValueError, match="WITNESS_DERIVATION_DRIFT"):
        gate.validate_final_subtitle_audio_check(captured, final_srt, media)


def test_validation_does_not_call_ffmpeg_or_provider(tmp_path: Path, monkeypatch) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    unavailable = gate.FinalSubtitleAudioGateAdapters(
        lambda *_args: (_ for _ in ()).throw(AssertionError("ffmpeg called")),
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider called")),
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider converter called")),
    )
    monkeypatch.setattr(gate, "_DEFAULT_ADAPTERS", unavailable)
    assert gate.validate_final_subtitle_audio_check(captured, final_srt, media)["status"] == "PASS"


def test_validated_witness_rejects_final_srt_media_and_intro_drift(
    tmp_path: Path,
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=6_200)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(
            witness_rows=[
                (start + 6_200, end + 6_200, text) for start, end, text in _ROWS
            ],
            calls={},
        ),
    )
    witness = gate.load_validated_subtitle_audio_witness(
        captured, final_srt, media, candidate_id="candidate"
    )
    assert witness["intro_offset_ms"] == 6_200
    original_srt = final_srt.read_bytes()

    final_srt.write_bytes(original_srt + b"\n")
    with pytest.raises(ValueError, match="FINAL_SRT_HASH_DRIFT"):
        gate.load_validated_subtitle_audio_witness(
            captured, final_srt, media, candidate_id="candidate"
        )
    final_srt.write_bytes(original_srt)

    other_media = tmp_path / "other.mp4"
    other_media.write_bytes(b"other media")
    with pytest.raises(ValueError, match="MEDIA_HASH_DRIFT"):
        gate.load_validated_subtitle_audio_witness(
            captured, final_srt, other_media, candidate_id="candidate"
        )

    captured["burned_preview"]["branding_intro"]["intro_offset_ms"] = 6_201
    with pytest.raises(ValueError, match="INTRO_OFFSET"):
        gate.load_validated_subtitle_audio_witness(
            captured, final_srt, media, candidate_id="candidate"
        )


_POSTBURN_ROWS = [
    (1_000, 5_000, "开场这句内容很特别"),
    (8_000, 12_000, "前段这里继续说明问题"),
    (18_000, 22_000, "中段这里保留完整语义"),
    (30_000, 34_000, "这里有乱码需要审看"),
    (45_000, 49_000, "最后我们明确完成收束"),
]
_POSTBURN_WITNESS_ROWS = [
    (start + 6_200, end + 6_200, text)
    for start, end, text in _POSTBURN_ROWS
]
_POSTBURN_WITNESS_ROWS[3] = (
    _POSTBURN_WITNESS_ROWS[3][0],
    _POSTBURN_WITNESS_ROWS[3][1],
    "这里有更正需要审看",
)


def _postburn_record(tmp_path: Path) -> tuple[dict, Path, Path]:
    record, final_srt, media = _record(
        tmp_path,
        intro_offset_ms=6_200,
        rows=_POSTBURN_ROWS,
    )
    return (
        gate.capture_final_subtitle_audio_check(
            record,
            final_srt,
            tmp_path / "evidence",
            "candidate",
            adapters=_adapters(
                witness_rows=_POSTBURN_WITNESS_ROWS,
                calls={},
            ),
        ),
        final_srt,
        media,
    )


def _postburn_chat_audit(final_srt: Path) -> dict:
    return {
        "final_review_audit": {
            "schema_version": "final-review-audit.v2",
            "reviewed_srt_sha256": _sha(final_srt),
            "status": "CLEAN",
            "release_gate": "PASS",
            "findings": [],
            "validated_finding_count": 0,
        }
    }


def _postburn_adapters(
    monkeypatch, response: str, calls: list[str] | None = None
) -> None:
    def llm(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        return response

    llm.cpa_cache_identity = {
        "transport": "cpa_command",
        "models": ["gpt-6-sol"],
        "effort": "medium",
    }
    llm.provider_runtime_binding = {
        "provider_transport": "runtime_cpa",
        "provider_credential_source": "/runtime/cpa.env",
    }
    monkeypatch.setattr(finalization, "build_final_review_llm_call", lambda: llm)


def test_postburn_witness_finding_blocks_without_mutating_final_srt(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    before_srt = final_srt.read_bytes()
    before_media = media.read_bytes()
    _postburn_adapters(
        monkeypatch,
        '{"findings":[{"cue":4,"kind":"context","repair_class":"phonetic",'
        '"proposed_full_cue":"这里有更正需要审看","source_surface":"更正",'
        '"why":"postburn witness disagreement"}]}',
    )
    chat_audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(
            cid="candidate",
            out_root=tmp_path,
            recut=finalization.FinalRecutArtifacts(
                recut_dir=tmp_path,
                media_path=media,
                subtitle_path=final_srt,
                text_manifest_path=None,
                text_manifest=None,
            ),
            record=record,
            chat_authority_audit=chat_audit,
            chat_authority_path=chat_path,
        )

    assert final_srt.read_bytes() == before_srt
    assert media.read_bytes() == before_media
    review = chat_audit["postburn_subtitle_audio_review"]
    assert review["status"] == "FLAGGED"
    assert review["findings"][0]["cue_index"] == 4
    assert review["subtitle_audio_binding"]["witness_srt_sha256"] == record[
        "subtitle_audio_correspondence"
    ]["witness_srt_sha256"]
    assert review["cpa_request"]["intro_offset_ms"] == 6_200
    assert review["cpa_request"]["audio_sha256"] == record[
        "subtitle_audio_correspondence"
    ]["audio_sha256"]
    assert review["cpa_request"]["raw_result_sha256"] == record[
        "subtitle_audio_correspondence"
    ]["raw_result_sha256"]
    assert review["cpa_cache_identity"]["effort"] == "medium"
    assert review["cpa_calls"][0]["response_sha256"].startswith("sha256:")
    assert Path(review["cpa_calls"][0]["prompt_path"]).read_text(encoding="utf-8")
    assert Path(review["cpa_calls"][0]["response_path"]).read_text(encoding="utf-8").startswith(
        '{"findings"'
    )
    assert (tmp_path / "candidate.review-flags.json").is_file()


def test_postburn_witness_clean_review_preserves_all_bindings(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    before_srt = final_srt.read_bytes()
    _postburn_adapters(monkeypatch, '{"findings":[]}')
    chat_audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")

    result = finalization._run_postburn_subtitle_audio_review(
        cid="candidate",
        out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=media,
            subtitle_path=final_srt,
            text_manifest_path=None,
            text_manifest=None,
        ),
        record=record,
        chat_authority_audit=chat_audit,
        chat_authority_path=chat_path,
    )

    assert result is record
    assert final_srt.read_bytes() == before_srt
    review = record["postburn_subtitle_audio_review"]
    assert review["status"] == "CLEAN"
    assert review["release_gate"] == "PASS"
    assert review["findings"] == []
    assert review["subtitle_audio_binding"]["actual_media_sha256"] == _sha(media)
    assert review["subtitle_audio_binding"]["final_srt_sha256"] == _sha(final_srt)
    assert review["cpa_request"]["witness_srt_sha256"] == record[
        "subtitle_audio_correspondence"
    ]["witness_srt_sha256"]
    assert review["cpa_request"]["receipt_sha256"] == record[
        "subtitle_audio_correspondence"
    ]["receipt_sha256"]
    assert review["cpa_calls"][0]["request_sha256"] == review["cpa_request"][
        "request_sha256"
    ]
    assert review["cpa_cache_identity"]["models"] == ["gpt-6-sol"]
    assert review["cpa_cache_identity"]["transport"] == "cpa_command"
    assert review["cpa_request"]["candidate_context_sha256"].startswith("sha256:")
    assert review["provider_runtime_binding"]["provider_transport"] == "runtime_cpa"
    assert review["provider_runtime_binding"]["provider_call_transport"] == "cpa_command"
    assert review["provider_runtime_binding"]["provider_model"] == ["gpt-6-sol"]
    assert review["provider_runtime_binding"]["provider_effort"] == "medium"


def test_postburn_review_reuses_valid_private_cpa_response(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    calls: list[str] = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', calls)
    chat_audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    kwargs = {
        "cid": "candidate",
        "out_root": tmp_path,
        "recut": finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=media,
            subtitle_path=final_srt,
            text_manifest_path=None,
            text_manifest=None,
        ),
        "record": record,
        "chat_authority_audit": chat_audit,
        "chat_authority_path": chat_path,
    }

    finalization._run_postburn_subtitle_audio_review(**kwargs)
    assert len(calls) == 1
    chat_audit = json.loads(chat_path.read_text(encoding="utf-8"))
    kwargs["chat_authority_audit"] = chat_audit
    first = chat_audit["final_review_audit"]["postburn_subtitle_audio_review"]
    assert first["cache_reused"] is False

    def unavailable(_prompt: str) -> str:
        raise AssertionError("valid postburn CPA cache must avoid provider replay")

    monkeypatch.setattr(finalization, "build_final_review_llm_call", lambda: unavailable)
    unavailable.cpa_cache_identity = {
        "transport": "cpa_command",
        "models": ["gpt-6-sol"],
        "effort": "medium",
    }
    unavailable.provider_runtime_binding = {
        "provider_transport": "runtime_cpa",
        "provider_credential_source": "/runtime/cpa.env",
    }
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    second = chat_audit["final_review_audit"]["postburn_subtitle_audio_review"]
    assert second["cache_reused"] is True
    assert second["cpa_calls"][0]["served_from_cache"] is True
    assert len(calls) == 1
    completed_audit = json.loads(chat_path.read_text(encoding="utf-8"))
    completed_postburn = completed_audit["final_review_audit"][
        "postburn_subtitle_audio_review"
    ]

    response_path = Path(second["cpa_calls"][0]["response_path"])
    original_response = response_path.read_bytes()
    response_path.write_text('{"findings":[]}\n', encoding="utf-8")
    unavailable_after_corruption = 0

    def unavailable_corrupt(_prompt: str) -> str:
        nonlocal unavailable_after_corruption
        unavailable_after_corruption += 1
        raise AssertionError("ambiguous cached dispatch must not be replayed")

    unavailable_corrupt.cpa_cache_identity = {
        "transport": "cpa_command",
        "models": ["gpt-6-sol"],
        "effort": "medium",
    }
    unavailable_corrupt.provider_runtime_binding = {
        "provider_transport": "runtime_cpa",
        "provider_credential_source": "/runtime/cpa.env",
    }
    monkeypatch.setattr(
        finalization, "build_final_review_llm_call", lambda: unavailable_corrupt
    )
    kwargs["selection_hook"] = ""
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    blocked = chat_audit["final_review_audit"]["postburn_subtitle_audio_review"]
    assert blocked["reason_codes"] == [
        "POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY"
    ]
    assert "prior_postburn_subtitle_audio_review" in blocked
    assert unavailable_after_corruption == 0

    response_path.write_bytes(original_response)
    changed_context_calls: list[str] = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', changed_context_calls)
    changed_audit = completed_audit
    changed_kwargs = {
        **kwargs,
        "chat_authority_audit": changed_audit,
        "chat_authority_path": tmp_path / "chat-changed.json",
        "selection_hook": "same artifacts, explicitly changed request",
    }
    changed_kwargs["chat_authority_path"].write_text(
        json.dumps(changed_audit, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    finalization._run_postburn_subtitle_audio_review(**changed_kwargs)
    changed = changed_audit["final_review_audit"]["postburn_subtitle_audio_review"]
    assert changed["cache_reused"] is False
    assert len(changed_context_calls) == 1
    assert changed["prior_postburn_subtitle_audio_review"] == completed_postburn


def test_postburn_checkpoint_blocks_replay_after_process_death(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    chat_audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    provider_calls = 0

    class DispatchProcessDeath(BaseException):
        pass

    def died_after_dispatch(_prompt: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        raise DispatchProcessDeath("provider process ended after dispatch")

    died_after_dispatch.cpa_cache_identity = {
        "transport": "cpa_command",
        "models": ["gpt-6-sol"],
        "effort": "medium",
    }
    died_after_dispatch.provider_runtime_binding = {
        "provider_transport": "runtime_cpa",
        "provider_credential_source": "/runtime/cpa.env",
    }
    monkeypatch.setattr(
        finalization,
        "build_final_review_llm_call",
        lambda: died_after_dispatch,
    )
    kwargs = {
        "cid": "candidate",
        "out_root": tmp_path,
        "recut": finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=media,
            subtitle_path=final_srt,
            text_manifest_path=None,
            text_manifest=None,
        ),
        "record": record,
        "chat_authority_audit": chat_audit,
        "chat_authority_path": chat_path,
    }

    with pytest.raises(DispatchProcessDeath):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    assert provider_calls == 1
    persisted = json.loads(chat_path.read_text(encoding="utf-8"))
    checkpoint = persisted["postburn_subtitle_audio_review"]
    assert checkpoint["status"] == "RUNNING"
    assert checkpoint["release_gate"] == "BLOCK"
    assert checkpoint["checkpoint"] == "before_provider_dispatch"
    assert checkpoint["cpa_calls"][0]["dispatch_status"] == "RUNNING"
    assert (
        checkpoint["cpa_calls"][0]["request_sha256"]
        == checkpoint["cpa_request"]["request_sha256"]
    )
    assert persisted["final_review_audit"]["release_gate"] == "PASS"
    review_flags = json.loads(
        (tmp_path / "candidate.review-flags.json").read_text(encoding="utf-8")
    )
    assert review_flags["postburn_subtitle_audio_review"]["status"] == "RUNNING"

    forbidden_calls = 0

    def forbidden_retry(_prompt: str) -> str:
        nonlocal forbidden_calls
        forbidden_calls += 1
        raise AssertionError("incomplete dispatch must not be retried")

    forbidden_retry.cpa_cache_identity = died_after_dispatch.cpa_cache_identity
    forbidden_retry.provider_runtime_binding = (
        died_after_dispatch.provider_runtime_binding
    )
    monkeypatch.setattr(finalization, "build_final_review_llm_call", lambda: forbidden_retry)
    kwargs["chat_authority_audit"] = persisted
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    blocked = persisted["final_review_audit"]["postburn_subtitle_audio_review"]
    assert blocked["reason_codes"] == [
        "POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY"
    ]
    assert blocked["cpa_calls"] == []
    assert blocked["prior_postburn_subtitle_audio_review"]["status"] == "RUNNING"
    assert forbidden_calls == 0


def test_postburn_review_rejects_missing_or_drifted_ordinary_audit(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    _postburn_adapters(monkeypatch, '{"findings":[]}')
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    kwargs = {
        "cid": "candidate",
        "out_root": tmp_path,
        "recut": finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=media,
            subtitle_path=final_srt,
            text_manifest_path=None,
            text_manifest=None,
        ),
        "record": record,
        "chat_authority_audit": {},
        "chat_authority_path": chat_path,
    }
    with pytest.raises(SystemExit, match="FINAL_REVIEW_AUDIT_MISSING"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)

    kwargs["chat_authority_audit"] = _postburn_chat_audit(final_srt)
    kwargs["chat_authority_audit"]["final_review_audit"]["reviewed_srt_sha256"] = _sha(
        media
    )
    with pytest.raises(SystemExit, match="FINAL_REVIEW_AUDIT_DRIFT"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)

    kwargs["chat_authority_audit"] = _postburn_chat_audit(final_srt)
    kwargs["chat_authority_audit"]["final_review_audit"]["release_gate"] = "BLOCK"
    with pytest.raises(SystemExit, match="FINAL_REVIEW_AUDIT_DRIFT"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)


def test_postburn_review_blocks_retry_after_incomplete_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    chat_audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    provider_calls = 0

    def failed_call(_prompt: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        raise RuntimeError("dispatch outcome unavailable")

    failed_call.cpa_cache_identity = {
        "transport": "cpa_command",
        "models": ["gpt-6-sol"],
        "effort": "medium",
    }
    failed_call.provider_runtime_binding = {
        "provider_transport": "runtime_cpa",
        "provider_credential_source": "/runtime/cpa.env",
    }
    monkeypatch.setattr(finalization, "build_final_review_llm_call", lambda: failed_call)
    kwargs = {
        "cid": "candidate",
        "out_root": tmp_path,
        "recut": finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=media,
            subtitle_path=final_srt,
            text_manifest_path=None,
            text_manifest=None,
        ),
        "record": record,
        "chat_authority_audit": chat_audit,
        "chat_authority_path": chat_path,
    }
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    assert provider_calls == 1
    chat_audit = json.loads(chat_path.read_text(encoding="utf-8"))
    kwargs["chat_authority_audit"] = chat_audit
    first = chat_audit["final_review_audit"]["postburn_subtitle_audio_review"]
    assert first["status"] == "AUDITOR_UNAVAILABLE"
    assert first["cpa_calls"][0]["error_type"] == "RuntimeError"
    ordinary = _postburn_chat_audit(final_srt)["final_review_audit"]
    ordinary["postburn_subtitle_audio_review"] = first
    chat_audit["final_review_audit"] = ordinary

    def forbidden_retry(_prompt: str) -> str:
        raise AssertionError("incomplete same-bound dispatch must not retry")

    forbidden_retry.cpa_cache_identity = failed_call.cpa_cache_identity
    forbidden_retry.provider_runtime_binding = failed_call.provider_runtime_binding
    monkeypatch.setattr(
        finalization, "build_final_review_llm_call", lambda: forbidden_retry
    )
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    second = chat_audit["final_review_audit"]["postburn_subtitle_audio_review"]
    assert second["reason_codes"] == [
        "POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY"
    ]
    assert second["cpa_calls"] == []
    assert second["prior_postburn_subtitle_audio_review"]["cpa_calls"][0][
        "error_type"
    ] == "RuntimeError"
    assert provider_calls == 1


def test_delivery_copies_all_four_gate_artifacts_under_delivery_name(
    tmp_path: Path, capsys
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    record = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    record["duration_ms"] = 46_000
    record_path = tmp_path / "candidate.record.json"
    record_path.write_text("{}\n", encoding="utf-8")
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    speaker_ass = tmp_path / "candidate.speaker.ass"
    speaker_ass.write_text("[Events]\n", encoding="utf-8")

    commands: list[list[str]] = []
    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda *_args, **_kwargs: [],
        run_command=lambda command, **_kwargs: commands.append(command),
        write_source_range_srt=lambda *_args, **_kwargs: None,
        apply_text_override_document=lambda *_args, **_kwargs: {},
        run_speaker_finalization=lambda *_args, **_kwargs: {},
        burn_preview_subtitles=lambda *_args, **_kwargs: record,
        stage_publish_draft=lambda *_args, **_kwargs: {},
        generate_upload_tags=lambda *_args, **_kwargs: {},
        delivery_root=lambda: tmp_path / "delivery",
    )
    result = finalization._deliver_staged_record(
        spec={"date": "2026-09-06", "delivery_name": "发布标题"},
        cid="candidate",
        final_end=46_000,
        audit={"closure_sentence": "收束", "verdict": "PASS", "red_flags": [], "boundary_repairs": []},
        timing_qa={"counts": {}},
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=media,
            subtitle_path=final_srt,
            text_manifest_path=None,
            text_manifest=None,
        ),
        speaker=finalization.SpeakerArtifacts(None, None, speaker_ass, None),
        authority=finalization.AuthorityArtifacts(None, None),
        chat_authority_path=chat_path,
        staged=finalization.StagedRecord(
            record=record,
            staging={"title": "发布标题"},
            record_path=record_path,
        ),
        adapters=adapters,
    )
    capsys.readouterr()
    assert result == 0
    destinations = {Path(command[-1]).name for command in commands}
    assert {
        "发布标题.subtitle-audio-witness.srt",
        "发布标题.subtitle-audio-provenance.json",
        "发布标题.subtitle-audio-correspondence.json",
        "发布标题.subtitle-audio-bcut.raw.json",
    } <= destinations


def test_prepared_delivery_declares_gate_artifacts_as_unique_roles(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    record = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    output_root = tmp_path / "runtime" / "out"
    output_root.mkdir(parents=True)
    record_path = tmp_path / "candidate.record.json"
    record_path.write_text("{}\n", encoding="utf-8")
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    speaker_ass = tmp_path / "candidate.speaker.ass"
    speaker_ass.write_text("[Events]\n", encoding="utf-8")
    seen: dict[str, object] = {}
    sentinel = object()
    monkeypatch.setattr(
        delivery_prepare,
        "prepare_delivery",
        lambda **kwargs: seen.update(kwargs) or sentinel,
    )
    monkeypatch.setattr(delivery_prepare, "deployment_authority_binding", lambda _root: {})

    result = delivery_prepare.prepare_talk_delivery(
        spec={
            "output_root": str(output_root),
            "date": "2026-09-06",
            "delivery_name": "发布标题",
        },
        candidate_id="candidate",
        record=record,
        staging={},
        record_path=record_path,
        subtitle_path=final_srt,
        speaker_review_srt=None,
        speaker_ass=speaker_ass,
        speaker_manifest_path=None,
        redelivery_baseline_audit_path=None,
        subtitle_regression_audit_path=None,
        chat_authority_path=chat_path,
        talk_filler_audit_path=None,
        text_manifest_path=None,
        delivery_root=tmp_path / "delivery",
    )
    assert result is sentinel
    artifacts = seen["artifacts"]
    gate_artifacts = {
        artifact.role: artifact for artifact in artifacts
        if artifact.role.startswith("subtitle_audio_")
    }
    assert set(gate_artifacts) == {
        "subtitle_audio_witness",
        "subtitle_audio_provenance",
        "subtitle_audio_correspondence",
        "subtitle_audio_raw_result",
    }
    assert {
        artifact.target.name for artifact in gate_artifacts.values()
    } == {
        "发布标题.subtitle-audio-witness.srt",
        "发布标题.subtitle-audio-provenance.json",
        "发布标题.subtitle-audio-correspondence.json",
        "发布标题.subtitle-audio-bcut.raw.json",
    }
