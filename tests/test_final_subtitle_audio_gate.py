from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.free_asr_client import to_srt
from src.autoslice import final_subtitle_audio_gate as gate
from src.autoslice import producer_delivery_prepare as delivery_prepare
from src.autoslice import producer_package_finalization as finalization
from src.autoslice import producer_text_pipeline as text_pipeline


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


_LEGACY_COMPOSITE_POLICY_KEYS = (
    "composite_enabled_when_single_coverage_insufficient",
    "composite_max_cues_per_side",
    "composite_gap_min_ms",
    "composite_gap_max_ms",
    "composite_match",
    "composite_counts_as_one_anchor",
    "composite_requires_unique_text",
)


_UNIQUENESS_POLICY_KEYS = ("anchor_uniqueness", "anchor_occurrence_match", "anchor_repeat_resolution")
_UNIQUENESS_ANCHOR_KEYS = ("occurrence_resolution", "occurrences_before", "occurrences_after")


def _strip_uniqueness_policy(receipt: dict) -> dict:
    """Return the shape the same inputs had before the uniqueness policy key."""

    legacy = copy.deepcopy(receipt)
    for key in _UNIQUENESS_POLICY_KEYS:
        legacy["anchor_policy"].pop(key)
    for anchor in legacy["anchors"]:
        for key in _UNIQUENESS_ANCHOR_KEYS:
            anchor.pop(key)
    return legacy


def _bind_receipt(record: dict, receipt: dict) -> None:
    evidence = record["subtitle_audio_correspondence"]
    evidence["receipt"] = receipt
    evidence["receipt_sha256"] = "sha256:" + hashlib.sha256(
        (json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    envelope_path = Path(evidence["correspondence_path"])
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["receipt"] = copy.deepcopy(receipt)
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _bind_legacy_single_anchor_receipt(record: dict) -> None:
    """Rewrite only the synthetic envelope to the pre-composite receipt shape."""

    evidence = record["subtitle_audio_correspondence"]
    legacy = _strip_uniqueness_policy(evidence["receipt"])
    legacy["anchor_coverage"].pop("cue_ids")
    for key in _LEGACY_COMPOSITE_POLICY_KEYS:
        legacy["anchor_policy"].pop(key)
    for anchor in legacy["anchors"]:
        for key in ("anchor_kind", "final_cue_indices", "witness_cue_indices"):
            anchor.pop(key)

    evidence["receipt"] = legacy
    evidence["receipt_sha256"] = "sha256:" + hashlib.sha256(
        (json.dumps(legacy, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()
    envelope_path = Path(evidence["correspondence_path"])
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["receipt"] = copy.deepcopy(legacy)
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
            delivery_lane="shadow",
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


def test_frozen_resume_accepts_only_known_receipt_metadata_growth(
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
    _bind_legacy_single_anchor_receipt(captured)

    with pytest.raises(ValueError, match="RECEIPT_DRIFT"):
        gate.validate_final_subtitle_audio_check(captured, final_srt, media)

    receipt = gate.validate_final_subtitle_audio_check(
        captured,
        final_srt,
        media,
        allow_frozen_receipt_metadata_compatibility=True,
    )
    assert receipt["status"] == "PASS"


def test_receipt_without_uniqueness_policy_replays_under_its_own_rule(tmp_path: Path) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    current = captured["subtitle_audio_correspondence"]["receipt"]
    assert current["anchor_policy"]["anchor_uniqueness"] == gate.ANCHOR_UNIQUENESS_POLICY
    _bind_receipt(captured, _strip_uniqueness_policy(current))

    # Ordinary validation (no frozen projection) accepts the pre-policy shape
    # because it is recomputed under the legacy rule it declares by omission.
    receipt = gate.validate_final_subtitle_audio_check(captured, final_srt, media)
    assert receipt["status"] == "PASS"
    assert "anchor_uniqueness" not in receipt["anchor_policy"]

    # A legacy-shaped receipt cannot claim anchors the legacy rule never chose.
    tampered = _strip_uniqueness_policy(current)
    tampered["anchors"][0]["occurrence_resolution"] = "unique"
    _bind_receipt(captured, tampered)
    with pytest.raises(ValueError, match="RECEIPT_DRIFT"):
        gate.validate_final_subtitle_audio_check(captured, final_srt, media)


def test_receipt_declaring_unknown_uniqueness_policy_is_refused(tmp_path: Path) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    receipt = copy.deepcopy(captured["subtitle_audio_correspondence"]["receipt"])
    receipt["anchor_policy"]["anchor_uniqueness"] = "relaxed-anything-goes"
    _bind_receipt(captured, receipt)

    with pytest.raises(ValueError, match="ANCHOR_UNIQUENESS_POLICY_UNSUPPORTED"):
        gate.validate_final_subtitle_audio_check(captured, final_srt, media)


def test_capture_upgrades_cached_legacy_receipt_without_provider_call(tmp_path: Path) -> None:
    record, final_srt, _media = _record(tmp_path, intro_offset_ms=0)
    calls: dict[str, int] = {}
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls=calls),
    )
    current = captured["subtitle_audio_correspondence"]["receipt"]
    _bind_receipt(captured, _strip_uniqueness_policy(current))

    def unavailable(*_args):
        raise AssertionError("a policy upgrade must reuse the cached BCUT observation")

    upgraded = gate.capture_final_subtitle_audio_check(
        captured,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=gate.FinalSubtitleAudioGateAdapters(unavailable, unavailable, unavailable),
    )["subtitle_audio_correspondence"]
    assert calls == {"extract": 1, "transcribe": 1, "to_srt": 1}
    assert upgraded["cache_reused"] is True
    assert upgraded["receipt"] == current
    envelope = json.loads(Path(upgraded["correspondence_path"]).read_text(encoding="utf-8"))
    assert envelope["receipt_policy_upgrade"]["provider_calls"] == 0
    assert envelope["receipt_policy_upgrade"]["from_anchor_uniqueness"] == (
        gate.anchor_uniqueness_policy_of({"anchor_policy": {}})
    )


@pytest.mark.parametrize("semantic_drift", [False, True])
def test_frozen_resume_validates_record_seal_with_relocatable_receipt_paths(
    tmp_path: Path, semantic_drift: bool,
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    _bind_legacy_single_anchor_receipt(captured)
    evidence = captured["subtitle_audio_correspondence"]
    evidence["receipt"]["inputs"]["final_srt"]["path"] = "/relocated/candidate.srt"
    if semantic_drift:
        evidence["receipt"]["timing_summary"]["median_start_delta_ms"] += 1
    evidence["receipt_sha256"] = "sha256:" + hashlib.sha256(
        (json.dumps(evidence["receipt"], ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()

    if semantic_drift:
        with pytest.raises(ValueError, match="RECORD_RECEIPT_DRIFT"):
            gate.validate_final_subtitle_audio_check(
                captured, final_srt, media,
                allow_frozen_receipt_metadata_compatibility=True,
            )
    else:
        receipt = gate.validate_final_subtitle_audio_check(
            captured, final_srt, media,
            allow_frozen_receipt_metadata_compatibility=True,
        )
        assert receipt["status"] == "PASS"


@pytest.mark.parametrize("mutation", ["timing", "source_index"])
def test_frozen_resume_rejects_legacy_semantic_changes(
    tmp_path: Path, mutation: str,
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    _bind_legacy_single_anchor_receipt(captured)
    receipt = captured["subtitle_audio_correspondence"]["receipt"]
    if mutation == "timing":
        receipt["timing_summary"]["median_start_delta_ms"] += 1
    else:
        receipt["anchors"][0]["final_cue_index"] = "changed"
    captured["subtitle_audio_correspondence"]["receipt_sha256"] = "sha256:" + hashlib.sha256(
        (json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()
    envelope_path = Path(captured["subtitle_audio_correspondence"]["correspondence_path"])
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["receipt"] = copy.deepcopy(receipt)
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="RECEIPT_DRIFT"):
        gate.validate_final_subtitle_audio_check(
            captured,
            final_srt,
            media,
            allow_frozen_receipt_metadata_compatibility=True,
        )


def test_frozen_resume_does_not_weaken_media_or_receipt_hash_gates(
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
    _bind_legacy_single_anchor_receipt(captured)

    other_media = tmp_path / "other.mp4"
    other_media.write_bytes(b"different media")
    with pytest.raises(ValueError, match="MEDIA_HASH_DRIFT"):
        gate.validate_final_subtitle_audio_check(
            captured,
            final_srt,
            other_media,
            allow_frozen_receipt_metadata_compatibility=True,
        )

    captured["subtitle_audio_correspondence"]["receipt_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="RECEIPT_HASH_DRIFT"):
        gate.validate_final_subtitle_audio_check(
            captured,
            final_srt,
            media,
            allow_frozen_receipt_metadata_compatibility=True,
        )


def test_frozen_resume_rejects_composite_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record,
        final_srt,
        tmp_path / "evidence",
        "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    current = copy.deepcopy(captured["subtitle_audio_correspondence"]["receipt"])
    _bind_legacy_single_anchor_receipt(captured)
    current["anchors"][0]["anchor_kind"] = "composite"
    current["anchors"][0]["final_cue_indices"] = ["1", "2"]
    current["anchors"][0]["witness_cue_indices"] = ["1", "2"]
    monkeypatch.setattr(
        gate,
        "check_subtitle_audio_correspondence",
        lambda *_args, **_kwargs: current,
    )

    with pytest.raises(ValueError, match="RECEIPT_DRIFT"):
        gate.validate_final_subtitle_audio_check(
            captured,
            final_srt,
            media,
            allow_frozen_receipt_metadata_compatibility=True,
        )


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


def test_validated_witness_reuses_captured_receipt_after_srt_relocation(
    tmp_path: Path,
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    calls: dict[str, int] = {}
    captured = gate.capture_final_subtitle_audio_check(
        record, final_srt, tmp_path / "evidence", "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls=calls),
    )
    original = gate.load_validated_subtitle_audio_witness(
        captured, final_srt, media, candidate_id="candidate"
    )
    relocated = tmp_path / "recovery" / "current.srt"
    relocated.parent.mkdir()
    relocated.write_bytes(final_srt.read_bytes())
    recovered = gate.load_validated_subtitle_audio_witness(
        captured, relocated, media, candidate_id="candidate"
    )
    assert recovered == original
    assert calls == {"extract": 1, "transcribe": 1, "to_srt": 1}
    relocated.write_bytes(relocated.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="FINAL_SRT_HASH_DRIFT"):
        gate.load_validated_subtitle_audio_witness(
            captured, relocated, media, candidate_id="candidate"
        )


@pytest.mark.parametrize("rebind_hash", [False, True])
def test_validated_witness_rejects_altered_record_receipt(
    tmp_path: Path, rebind_hash: bool,
) -> None:
    record, final_srt, media = _record(tmp_path, intro_offset_ms=0)
    captured = gate.capture_final_subtitle_audio_check(
        record, final_srt, tmp_path / "evidence", "candidate",
        adapters=_adapters(witness_rows=_ROWS, calls={}),
    )
    evidence = captured["subtitle_audio_correspondence"]
    evidence["receipt"]["timebase"]["intro_offset_ms"] = 1
    if rebind_hash:
        evidence["receipt_sha256"] = "sha256:" + hashlib.sha256(
            (json.dumps(evidence["receipt"], ensure_ascii=False, sort_keys=True)
             + "\n").encode("utf-8")
        ).hexdigest()
    expected = "RECORD_RECEIPT_DRIFT" if rebind_hash else "RECORD_RECEIPT_HASH_DRIFT"
    with pytest.raises(ValueError, match=expected):
        gate.load_validated_subtitle_audio_witness(
            captured, final_srt, media, candidate_id="candidate"
        )


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
# Cue 4 reads 更正 in the witness but 乱码 in the final. The deterministic
# witness-disagreement nomination now turns that into an exact-final question
# (see tests/test_witness_disagreement.py); the scripted-CPA fixtures below use
# a pinyin-equal witness so they keep exercising CPA-discovered findings only.
_POSTBURN_DISAGREEING_WITNESS_ROWS = [
    (start, end, "这里有更正需要审看" if index == 3 else text)
    for index, (start, end, text) in enumerate(_POSTBURN_WITNESS_ROWS)
]
_POSTBURN_WITNESS_ROWS[3] = (
    _POSTBURN_WITNESS_ROWS[3][0],
    _POSTBURN_WITNESS_ROWS[3][1],
    "这里有乱马需要审看",
)


def _postburn_record(tmp_path: Path, *, disagreeing: bool = False) -> tuple[dict, Path, Path]:
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
                witness_rows=(
                    _POSTBURN_DISAGREEING_WITNESS_ROWS if disagreeing else _POSTBURN_WITNESS_ROWS
                ),
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
        '"why":"postburn witness disagreement","content_risk":"meaning_reversal",'
        '"risk_basis":"This synthetic substitution changes the described text."}]}',
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


def test_completed_postburn_finding_is_only_a_bound_priority_raw_row(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    _postburn_adapters(
        monkeypatch,
        '{"findings":[{"cue":4,"kind":"context","repair_class":"phonetic",'
        '"proposed_full_cue":"这里有更正需要审看","source_surface":"更正",'
        '"why":"postburn witness disagreement","content_risk":"meaning_reversal",'
        '"risk_basis":"This synthetic substitution changes the described text."}]}',
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

    result = text_pipeline.validated_postburn_priority_findings(
        final_srt_text=final_srt.read_text(encoding="utf-8"),
        verified_authority_audit=chat_audit,
        candidate_id="candidate",
        clip_context=None,
        expected_media_sha256=_sha(media),
    )
    assert result is not None
    rows, request_sha = result
    assert request_sha == chat_audit["postburn_subtitle_audio_review"][
        "cpa_request"
    ]["request_sha256"]
    assert rows[0]["cue_index"] == 4
    assert rows[0]["base_text_sha256"] == hashlib.sha256(
        "这里有乱码需要审看".encode("utf-8")
    ).hexdigest()
    assert "exact_release_adjudication" not in rows[0]


def test_postburn_priority_handoff_rejects_drift_or_incomplete_raw_receipts(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    _postburn_adapters(
        monkeypatch,
        '{"findings":[{"cue":4,"kind":"context","repair_class":"phonetic",'
        '"proposed_full_cue":"这里有更正需要审看","source_surface":"更正",'
        '"why":"postburn witness disagreement","content_risk":"meaning_reversal",'
        '"risk_basis":"This synthetic substitution changes the described text."}]}',
    )
    chat_audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
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
    kwargs = {
        "final_srt_text": final_srt.read_text(encoding="utf-8"),
        "verified_authority_audit": chat_audit,
        "candidate_id": "candidate",
        "clip_context": None,
        "expected_media_sha256": _sha(media),
    }
    assert text_pipeline.validated_postburn_priority_findings(**kwargs) is not None

    chat_audit["postburn_subtitle_audio_review"]["cpa_request"][
        "candidate_context_sha256"
    ] = "sha256:" + "f" * 64
    assert text_pipeline.validated_postburn_priority_findings(**kwargs) is None
    chat_audit["postburn_subtitle_audio_review"]["cpa_request"][
        "candidate_context_sha256"
    ] = "sha256:" + hashlib.sha256(b"").hexdigest()
    chat_audit["postburn_subtitle_audio_review"]["reviewed_srt_sha256"] = _sha(media)
    assert text_pipeline.validated_postburn_priority_findings(**kwargs) is None
    chat_audit["postburn_subtitle_audio_review"]["reviewed_srt_sha256"] = _sha(
        final_srt
    )
    response_path = Path(
        chat_audit["postburn_subtitle_audio_review"]["cpa_calls"][0][
            "response_path"
        ]
    )
    response_path.unlink()
    assert text_pipeline.validated_postburn_priority_findings(**kwargs) is None


@pytest.mark.parametrize("actionable", [False, True])
@pytest.mark.parametrize("omit_descriptor", [False, True])
@pytest.mark.parametrize("legacy_context", [False, True])
@pytest.mark.parametrize("risk", ["noncritical_uncertainty", "meaning_reversal", "factual_error", "potential_harm"])
def test_postburn_current_risk_discloses_only_noncritical_and_reuses_raw_response(tmp_path, monkeypatch, risk, legacy_context, omit_descriptor, actionable):
    # Historical node ID retained; noncritical disclosure is no longer a
    # release exit. A complete equivalence assessment must still be FLAGGED.
    record, final_srt, media = _postburn_record(tmp_path)
    before = (final_srt.read_bytes(), media.read_bytes())
    calls = []
    proposed = "这里有乱码要审看" if risk == "noncritical_uncertainty" else "这里有更正需要审看"
    row = {
        "cue": 4, "kind": "context", "repair_class": "phonetic",
        "proposed_full_cue": proposed if actionable else None, "source_surface": "更正",
        "why": "postburn witness uncertainty", "content_risk": risk,
        "risk_basis": "This current synthetic review classifies the effect of the uncertainty.",
    }
    if risk == "noncritical_uncertainty":
        row["meaning_equivalence"] = {
            "status": "EQUIVALENT", "current_meaning": "此处文字需要审看。",
            "alternative_meaning": "此处文字要审看。", "shared_meaning": "此处文字仍待审看。",
            "uncertain_detail": "需要或要的原话差异。",
            "context_basis": "本合成例第4句两种词面均提出审看，不改变对象及动作。",
            "evidence_cue_ids": [4],
        }
    _postburn_adapters(monkeypatch, json.dumps({"findings": [row]}), calls)
    audit = _postburn_chat_audit(final_srt)
    preburn = copy.deepcopy(audit["final_review_audit"])
    kwargs = dict(cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, final_srt, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=tmp_path / "chat.json")
    kwargs["typed_resolution_evidence"] = {"reviewed_srt_sha256": _sha(final_srt),
        "resolved_findings": [{"cue_index": 4, "content_risk": "noncritical_uncertainty",
            "risk_basis": "first explanation", "_candidate_pronoun_policy_audit": {"completion_sha256": "first"}}]}
    if omit_descriptor:
        kwargs["typed_resolution_evidence"]["resolved_findings"].append({
            "cue_index": 1, "content_risk": "noncritical_uncertainty", "risk_basis": "ordinary old descriptor"})
    canonical_progress = finalization._postburn_material_progress
    if legacy_context:
        monkeypatch.setattr(finalization, "_postburn_material_progress", lambda value: value)
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    receipt = audit["postburn_subtitle_audio_review"]
    assert receipt["status"] == "FLAGGED" and receipt["release_gate"] == "BLOCK"
    assert receipt["unresolved_findings_disclosed"] == []
    assert receipt["findings"][0]["content_risk"] == risk
    first_calls = len(calls)
    original_request_sha = receipt["cpa_request"]["request_sha256"]
    monkeypatch.setattr(finalization, "_postburn_material_progress", canonical_progress)
    kwargs["typed_resolution_evidence"]["resolved_findings"][0].update(
        risk_basis="equivalent new explanation",
        _candidate_pronoun_policy_audit={"completion_sha256": "fresh-response-hash"},
    )
    if omit_descriptor:
        kwargs["typed_resolution_evidence"]["resolved_findings"].pop()
    # Normal continuation restores the verified preburn input and retains the
    # completed postburn raw response. This cannot sign a new release PASS.
    audit["final_review_audit"] = {**preburn, "postburn_subtitle_audio_review": receipt}
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    assert len(calls) == first_calls
    assert audit["postburn_subtitle_audio_review"]["cache_reused"] is True
    assert audit["postburn_subtitle_audio_review"]["release_gate"] == "BLOCK"
    assert audit["postburn_subtitle_audio_review"]["cpa_request"]["request_sha256"] == original_request_sha
    assert (final_srt.read_bytes(), media.read_bytes()) == before


@pytest.mark.parametrize("cache_damage", [False, True])
def test_postburn_observation_survives_restoring_preburn_input(tmp_path, monkeypatch, cache_damage):
    record, final_srt, media = _postburn_record(tmp_path)
    calls = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', calls)
    kwargs = dict(cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, final_srt, None, None),
        record=record, chat_authority_audit=_postburn_chat_audit(final_srt),
        chat_authority_path=tmp_path / "candidate.chat-authority.json")
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    kwargs["chat_authority_audit"] = _postburn_chat_audit(final_srt)
    if cache_damage:
        pointer = json.loads((tmp_path / '.candidate.postburn-cpa/observation.json').read_text())
        Path(pointer['path']).write_text('{}')
        with pytest.raises(ValueError, match="HASH_DRIFT"):
            finalization._run_postburn_subtitle_audio_review(**kwargs)
    else:
        finalization._run_postburn_subtitle_audio_review(**kwargs)
        assert kwargs['chat_authority_audit']['postburn_subtitle_audio_review']['cache_reused']
    assert len(calls) == 1


@pytest.mark.parametrize("damage", [None, "latest_failed_pointer", "new_context", "exit", "prompt", "media", "execution_hash", "raw_json", "critical"])
def test_completed_child_raw_requires_execution_binding_and_normal_parser(tmp_path, monkeypatch, damage):
    record, final_srt, media = _postburn_record(tmp_path)
    calls = []
    response = ('{"findings":[{"cue":4,"kind":"context","repair_class":"phonetic",'
        '"proposed_full_cue":"这里有更正需要审看","source_surface":"更正",'
        '"why":"concrete critical conflict","content_risk":"meaning_reversal",'
        '"risk_basis":"The stated subject reverses the current meaning."}]}') if damage == "critical" else '{"findings":[]}'
    _postburn_adapters(monkeypatch, response, calls)
    audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / 'candidate.chat-authority.json'
    kwargs = dict(cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, final_srt, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=chat_path)
    try:
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    except SystemExit:
        assert damage == 'critical'
    review = copy.deepcopy(audit['postburn_subtitle_audio_review'])
    call = review['cpa_calls'][0]
    files = []
    for name, key in [('prompt.txt', 'prompt_path'), ('completion.txt', 'response_path')]:
        raw_path = Path(call[key])
        if name == 'prompt.txt' and damage == 'prompt':
            raw_path = tmp_path / 'different-prompt.txt'
            raw_path.write_text('different request')
        if name == 'completion.txt' and damage == 'raw_json':
            raw_path = tmp_path / 'invalid-completion.txt'
            raw_path.write_text('invalid raw output')
        files.append({'source': '/native/bridge/' + name, 'preserved': str(raw_path),
            'sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest(), 'bytes': raw_path.stat().st_size})
    execution = {'status': 'INTERRUPTED', 'output_root': str(tmp_path),
        'terminal_materials': {'srt': {'sha256': _sha(final_srt)[7:]},
                              'video': {'sha256': ('0' * 64 if damage == 'media' else _sha(media)[7:])}},
        'interruption': {'provider_child_completed_before_parent_stop': True,
            'raw_completion_unconsumed': {'outcome': 'CHILD_COMPLETED_RAW_UNCONSUMED_PARENT_INTERRUPTED',
                'raw_wait_exit_code': 1 if damage == 'exit' else 0,
                'child_not_signalled': True, 'pidfd_exit_ready': True, 'files': files}}}
    execution_path = tmp_path / 'execution.json'
    execution_path.write_text(json.dumps(execution))
    monkeypatch.setenv('AUTOSLICE_COMPLETED_CPA_EXECUTION', str(execution_path))
    monkeypatch.setenv('AUTOSLICE_COMPLETED_CPA_EXECUTION_SHA256',
        '0' * 64 if damage == 'execution_hash' else hashlib.sha256(execution_path.read_bytes()).hexdigest())
    review.update(status='RUNNING', release_gate='BLOCK', findings=[], validated_finding_count=0)
    for key in ['response_path', 'response_sha256']:
        call.pop(key)
    call['dispatch_status'] = 'RUNNING'
    audit['postburn_subtitle_audio_review'] = review
    chat_path.write_text(json.dumps(audit))
    before = chat_path.read_bytes()
    if damage in {'exit', 'prompt', 'media', 'execution_hash'}:
        with pytest.raises(ValueError, match='(BINDING_INVALID|REQUEST_INVALID|HASH_DRIFT)'):
            finalization.preserve_postburn_observation(tmp_path, 'candidate')
    else:
        finalization.preserve_postburn_observation(tmp_path, 'candidate')
        assert chat_path.read_bytes() == before
        if damage == 'latest_failed_pointer':
            audit['postburn_subtitle_audio_review'] = {**review, 'status': 'AUDITOR_UNAVAILABLE',
                'error_type': 'ValueError', 'cpa_calls': [], 'reason_codes': ['POSTBURN_COMPLETED_RAW_REQUIRES_ORIGINAL_INPUT']}
            chat_path.write_text(json.dumps(audit))
            finalization.preserve_postburn_observation(tmp_path, 'candidate')
        pointer = tmp_path / '.candidate.postburn-cpa/observation.json'
        pointer_bytes = pointer.read_bytes()
        saved = finalization._load_postburn_observation(tmp_path, 'candidate')
        assert pointer.read_bytes() == pointer_bytes
        assert saved['status'] == 'RUNNING' and saved['release_gate'] == 'BLOCK'
        kwargs['chat_authority_audit'] = _postburn_chat_audit(final_srt)
        if damage == 'new_context':
            kwargs['selection_hook'] = 'genuinely different source context'
        if damage in {'raw_json', 'critical', 'new_context'}:
            with pytest.raises(SystemExit, match='POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED'):
                finalization._run_postburn_subtitle_audio_review(**kwargs)
        else:
            finalization._run_postburn_subtitle_audio_review(**kwargs)
            assert kwargs['chat_authority_audit']['postburn_subtitle_audio_review']['cached_parser_recovery']
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['critical', 'media', 'audio'])
def test_postburn_metadata_carry_requires_ordinary_same_material_descriptor(tmp_path, monkeypatch, change):
    record, final_srt, media = _postburn_record(tmp_path)
    calls = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', calls)
    row = {'cue_index': 1, 'content_risk': 'meaning_reversal' if change == 'critical' else 'noncritical_uncertainty',
           'risk_basis': 'A concrete effect stated by the previous current review.'}
    audit = _postburn_chat_audit(final_srt)
    finalization._run_postburn_subtitle_audio_review(cid='candidate', out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, final_srt, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=tmp_path/'chat.json',
        typed_resolution_evidence={'reviewed_srt_sha256': _sha(final_srt), 'resolved_findings': [row]})
    previous = audit['postburn_subtitle_audio_review']
    witness = dict(previous['cpa_request'])
    if change != 'critical':
        witness['actual_media_sha256' if change == 'media' else 'audio_sha256'] = 'sha256:' + 'f' * 64
    current = {'reviewed_srt_sha256': _sha(final_srt), 'resolved_findings': []}
    assert finalization._postburn_carry_disclosure_metadata(current, previous, _sha(final_srt), witness=witness) == current
    assert len(calls) == 1


def test_postburn_witness_clean_review_preserves_all_bindings(
    tmp_path: Path, monkeypatch
) -> None:
    record, final_srt, media = _postburn_record(tmp_path)
    record["chat_authority_audit_path"] = str(tmp_path / "prior-chat.json")
    before_srt = final_srt.read_bytes()
    prompts: list[str] = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', prompts)
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
    assert Path(record["chat_authority_audit_path"]) == chat_path
    assert record["artifact_hashes"]["chat_authority_audit_sha256"] == _sha(chat_path)
    assert final_srt.read_bytes() == before_srt
    review = record["postburn_subtitle_audio_review"]
    assert review["status"] == "CLEAN"
    assert "当前候选相关的频道术语" in prompts[0]
    assert "侄女" in prompts[0]
    assert review["cpa_request"]["glossary_sha256"].startswith("sha256:")
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


@pytest.mark.parametrize("drift", [None, "audio", "source", "offset"])
def test_postburn_replays_current_bound_source_context(tmp_path, monkeypatch, drift):
    from tests.test_independent_context_adjudication import _native_range_artifacts
    from src.autoslice.jingting_chunker import parse_srt_cues

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    text, _finding, context, _request, observation, paths = _native_range_artifacts(
        source_dir, "moss",
    )
    # The current wording may differ from the saved observation. Keep its
    # grid, and supply enough unique text for the real timing gate's anchors.
    text = text.replace("第一句", "第一句开场完整介绍当前情景").replace(
        "当前句", "当前句中段继续解释事件经过",
    ).replace("第三句", "第三句最后说明完整事件结论")
    rows = [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_srt_cues(text)]
    record, final_srt, media = _record(tmp_path, intro_offset_ms=6_200, rows=rows)
    final_srt.write_text(text, encoding="utf-8")
    record = gate.capture_final_subtitle_audio_check(
        record, final_srt, tmp_path / "evidence", "candidate",
        adapters=_adapters(
            witness_rows=[(start + 6_200, end + 6_200, value) for start, end, value in rows],
            calls={},
        ),
    )
    if drift in {"audio", "source"}:
        paths["audio_clip" if drift == "audio" else "source_media"].write_bytes(b"drift")
    prompts = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', prompts)
    monkeypatch.setattr(finalization, "clip_context_prompt_text", lambda _context: "scene")
    audit = _postburn_chat_audit(final_srt)
    before = final_srt.read_bytes(), media.read_bytes()
    finalization._run_postburn_subtitle_audio_review(
        cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path, media_path=media, subtitle_path=final_srt,
            text_manifest_path=None, text_manifest=None,
        ),
        record=record, chat_authority_audit=audit, chat_authority_path=tmp_path / "chat.json",
        clip_context=context, timeline_offset_ms=101 if drift == "offset" else 100,
        source_evidence_spec={"prior_range_acoustic_observations": [observation, observation]},
    )
    projected = audit["postburn_subtitle_audio_review"]["source_context_discovery_audit"]
    assert projected["range_count"] == (0 if drift else 1)
    assert projected["current_srt_sha256"] == _sha(final_srt)
    assert projected["provider_calls"] == 0 and projected["mutation_authorized"] is False
    assert ("整句范围听写" in prompts[0]) == (drift is None)
    assert (final_srt.read_bytes(), media.read_bytes()) == before


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



def test_postburn_parser_recovery_preserves_original_doubt_without_new_dispatch(
    tmp_path: Path, monkeypatch, mode=None
) -> None:
    from src.autoslice.final_review_auditor import FinalReviewAuditError
    from src.autoslice.final_review_discovery_prompt import (
        FINAL_SUBTITLE_AUDIT_PROMPT, LEGACY_FINAL_SUBTITLE_AUDIT_PROMPT)
    from src.autoslice.final_review_schema_retry import schema_repair_prompt
    from src.autoslice.jingting_chunker import parse_srt_cues

    if mode is None:
        monkeypatch.delenv('AUTOSLICE_FINAL_REVIEW_MODE', raising=False)
    else:
        monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE', mode)
    prompt_template = (FINAL_SUBTITLE_AUDIT_PROMPT if mode == 'per_finding'
                       else LEGACY_FINAL_SUBTITLE_AUDIT_PROMPT)
    record, final_srt, media = _postburn_record(tmp_path)
    original_bytes = final_srt.read_bytes()
    calls = []
    response = json.dumps({"findings": [{
        "cue": 4, "kind": "context", "repair_class": "disclosure_only",
        "proposed_full_cue": None, "why": "无安全候选，需补听",
        "content_risk": "meaning_reversal",
        "risk_basis": "The synthetic current phrase has unresolved meaning damage.",
    }]}, ensure_ascii=False)
    _postburn_adapters(monkeypatch, response, calls)
    actual_auditor = finalization.audit_final_subtitles

    def old_parser(srt_text, *, llm_call, **kwargs):
        from src.autoslice.surface_canon import CHANNEL_PROFILE
        prompt = prompt_template.format(
            host_name=CHANNEL_PROFILE.display_name,
            host_short=CHANNEL_PROFILE.short_name,
            max_findings=24,
            numbered="\n".join(f"{i}. {cue.text}" for i, cue in enumerate(parse_srt_cues(srt_text), 1)),
            glossary=kwargs["glossary_text"].strip() or "（无）",
            structured_context=kwargs["structured_context_text"].strip() or "（无）",
            candidate_context=kwargs["candidate_context_text"].strip() or "（无）",
        )
        llm_call(prompt)
        llm_call(schema_repair_prompt(prompt, '{"invalid_rows":[{"cue":4}]}'))
        raise FinalReviewAuditError("FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID", "old parser rejected explicit null")

    monkeypatch.setattr(finalization, "audit_final_subtitles", old_parser)
    chat_audit = _postburn_chat_audit(final_srt)
    kwargs = dict(
        cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, final_srt, None, None),
        record=record, chat_authority_audit=chat_audit,
        chat_authority_path=tmp_path / "chat.json",
    )
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    failed = chat_audit["postburn_subtitle_audio_review"]
    assert len(calls) == 2
    assert failed["status"] == "AUDITOR_UNAVAILABLE"
    # Preserve the actual ordinary exact-final input separately from the
    # failed discovery envelope, as the normal continuation consumer does.
    ordinary = _postburn_chat_audit(final_srt)["final_review_audit"]
    ordinary["postburn_subtitle_audio_review"] = failed
    chat_audit["final_review_audit"] = ordinary
    monkeypatch.setattr(finalization, "audit_final_subtitles", actual_auditor)
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    recovered = chat_audit["postburn_subtitle_audio_review"]
    assert recovered["cached_parser_recovery"] is True
    assert recovered["status"] == "FLAGGED"
    assert recovered["release_gate"] == "BLOCK"
    assert recovered["findings"][0]["suspect"] == "这里有乱码需要审看"
    assert recovered["findings"][0]["suggestion"] is None
    assert len(recovered["cpa_calls"]) == 1
    assert recovered["prior_postburn_subtitle_audio_review"] == failed
    assert len(recovered["prior_postburn_subtitle_audio_review"]["cpa_calls"]) == 2
    assert len(calls) == 2
    assert final_srt.read_bytes() == original_bytes


def test_per_finding_postburn_parser_recovery_preserves_original_doubt_without_new_dispatch(tmp_path, monkeypatch):
    test_postburn_parser_recovery_preserves_original_doubt_without_new_dispatch(
        tmp_path, monkeypatch, mode='per_finding')

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


def test_postburn_template_change_is_a_new_bound_question_and_then_reuses(tmp_path, monkeypatch):
    from copy import deepcopy
    from src.autoslice import final_review_auditor as auditor

    record, srt, media = _postburn_record(tmp_path)
    calls = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', calls)
    audit = _postburn_chat_audit(srt)
    kwargs = dict(cid='candidate', out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, srt, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=tmp_path/'chat.json')
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    first = deepcopy(audit['postburn_subtitle_audio_review'])
    files = {Path(row[key]): Path(row[key]).read_bytes()
             for row in first['cpa_calls'] for key in ('prompt_path', 'response_path')}
    monkeypatch.setattr(auditor, '_AUDIT_PROMPT', auditor._AUDIT_PROMPT + '\nIndependent discovery policy revision.\n')
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    second = deepcopy(audit['postburn_subtitle_audio_review'])
    assert len(calls) == 2 and not second['cache_reused']
    assert second['cpa_request']['request_sha256'] != first['cpa_request']['request_sha256']
    assert second['prior_postburn_subtitle_audio_review'] == first
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    assert len(calls) == 2 and audit['postburn_subtitle_audio_review']['cache_reused']
    assert all(path.read_bytes() == raw for path, raw in files.items())


@pytest.mark.parametrize('damage', [None, 'request', 'dispatch', 'outcome', 'raw'])
def test_zero_dispatch_parser_failure_retains_only_its_verified_completed_parent(tmp_path, monkeypatch, damage):
    from copy import deepcopy

    record, srt, media = _postburn_record(tmp_path)
    calls = []
    _postburn_adapters(monkeypatch, '{"findings":[]}', calls)
    audit = _postburn_chat_audit(srt)
    kwargs = dict(cid='candidate', out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, srt, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=tmp_path/'chat.json')
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    parent = deepcopy(audit['postburn_subtitle_audio_review'])
    failed = {**deepcopy(parent), 'status':'AUDITOR_UNAVAILABLE', 'release_gate':'BLOCK',
        'reason_codes':['POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY'],
        'error_type':'ValueError', 'cpa_calls':[], 'prior_postburn_subtitle_audio_review':parent}
    if damage == 'request':
        failed['cpa_request']['candidate_id'] = 'other'
    elif damage == 'dispatch':
        failed['cpa_calls'] = [{'dispatch_status':'RUNNING'}]
    elif damage == 'outcome':
        failed['status'] = 'OUTCOME_UNKNOWN'
    elif damage == 'raw':
        Path(parent['cpa_calls'][0]['response_path']).write_text('changed')
    selected = finalization._completed_postburn_before_parser_failure(failed)
    assert (selected is parent) == (damage is None)
    assert len(calls) == 1
    if damage is None:
        # The normal snapshot writer compacts the envelope, retaining its exact
        # full authority. The reader recovers that parent with no inferred PASS.
        audit['postburn_subtitle_audio_review'] = failed
        (tmp_path/'candidate.chat-authority.json').write_text(json.dumps(audit))
        finalization.preserve_postburn_observation(tmp_path, 'candidate')
        restored = finalization._load_postburn_observation(tmp_path, 'candidate')
        assert restored == failed
        assert finalization._completed_postburn_before_parser_failure(restored) == parent
