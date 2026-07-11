import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.produce_slice_package import run_speaker_finalizer

from src.autoslice.speaker_finalizer import (
    SOURCE_SESSION_ANCHOR_SCHEMA,
    SpeakerFinalizationError,
    _assert_runtime_assets_stable,
    _context_prompt,
    _load_source_session_anchor_samples,
    _pair_cache_key,
    _speaker_context_env,
    _validate_source_session_anchor_document,
    finalize_speaker_subtitles,
    resolve_ambiguous_labels,
)
from src.autoslice.host_vocal_proof import _sha256_directory


def _source_session_document() -> dict:
    anchors = []
    for cue_index in (9, 11):
        anchors.append(
            {
                "source_cue": cue_index,
                "start": "00:00:01,000",
                "end": "00:00:03,000",
                "text": "李豆沙 donor",
                "sample_sha256": str(cue_index)[0] * 64,
                "reference_scores": {"r1": 0.72, "r2": 0.74, "r3": 0.76},
                "enroll_median_score": 0.74,
            }
        )
    return {
        "schema_version": SOURCE_SESSION_ANCHOR_SCHEMA,
        "status": "READY",
        "subject": "李豆沙",
        "source_session_id": "session-1",
        "source_recording": "/remote/session.mp4",
        "profile_sha256": "a" * 64,
        "model_tree_sha256": "b" * 64,
        "reference_hashes": {"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
        "allowed_targets": [
            {
                "candidate_id": "target-1",
                "media_path": "/remote/target.mp4",
                "media_sha256": "f" * 64,
                "provenance_path": "/remote/target-spec.json",
                "provenance_sha256": "3" * 64,
            }
        ],
        "donor": {
            "candidate_id": "donor-1",
            "media_path": "/remote/donor.mp4",
            "media_sha256": "1" * 64,
            "text_srt_path": "/remote/donor.srt",
            "text_srt_sha256": "2" * 64,
            "provenance_path": "/remote/donor-spec.json",
            "provenance_sha256": "4" * 64,
        },
        "anchors": anchors,
    }


def test_pair_cache_key_is_symmetric_and_model_bound() -> None:
    assert _pair_cache_key("model-a", "left", "right") == _pair_cache_key("model-a", "right", "left")
    assert _pair_cache_key("model-a", "left", "right") != _pair_cache_key("model-b", "left", "right")


def test_runtime_model_and_reference_assets_are_rehashed_before_ready(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    model_file = model_dir / "weights.bin"
    model_file.write_bytes(b"model-a")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference-a")
    rows = [
        {
            "id": "lidousha-1",
            "path": reference,
            "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        }
    ]
    model_hash = _sha256_directory(model_dir)
    _assert_runtime_assets_stable(
        model_dir=model_dir,
        model_tree_sha256=model_hash,
        references=rows,
    )
    model_file.write_bytes(b"model-b")
    with pytest.raises(SpeakerFinalizationError, match="model tree drifted"):
        _assert_runtime_assets_stable(
            model_dir=model_dir,
            model_tree_sha256=model_hash,
            references=rows,
        )
    model_file.write_bytes(b"model-a")
    reference.write_bytes(b"reference-b")
    with pytest.raises(SpeakerFinalizationError, match="voiceprint reference drifted"):
        _assert_runtime_assets_stable(
            model_dir=model_dir,
            model_tree_sha256=model_hash,
            references=rows,
        )


def test_source_session_anchors_are_target_bound_and_keep_the_original_high_gate() -> None:
    document = _source_session_document()
    validated = _validate_source_session_anchor_document(
        document,
        target_media_sha256="f" * 64,
        profile_sha256="a" * 64,
        model_tree_sha256="b" * 64,
        reference_hashes={"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
        host_seed_min=0.68,
    )
    assert validated["source_session_id"] == "session-1"

    with pytest.raises(SpeakerFinalizationError, match="not allowlisted"):
        _validate_source_session_anchor_document(
            document,
            target_media_sha256="0" * 64,
            profile_sha256="a" * 64,
            model_tree_sha256="b" * 64,
            reference_hashes={"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
            host_seed_min=0.68,
        )

    document["anchors"][0]["reference_scores"] = {"r1": 0.40, "r2": 0.42, "r3": 0.44}
    document["anchors"][0]["enroll_median_score"] = 0.42
    with pytest.raises(SpeakerFinalizationError, match="unchanged host seed gate"):
        _validate_source_session_anchor_document(
            document,
            target_media_sha256="f" * 64,
            profile_sha256="a" * 64,
            model_tree_sha256="b" * 64,
            reference_hashes={"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
            host_seed_min=0.68,
        )


def test_source_session_loader_creates_donor_workdir_and_replays_hash_bound_cues(
    tmp_path: Path, monkeypatch,
) -> None:
    source_recording_path = tmp_path / "source-recording.mp4"
    source_recording_path.write_bytes(b"source-recording")
    source_recording = str(source_recording_path)
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    target_media = tmp_path / "target.mp4"
    target_media.write_bytes(b"target-media")
    donor_media = tmp_path / "donor.mp4"
    donor_media.write_bytes(b"donor-media")
    donor_srt = tmp_path / "donor.srt"
    donor_srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nanchor one\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nanchor two\n",
        encoding="utf-8",
    )
    donor_spec = tmp_path / "donor-spec.json"
    donor_spec.write_text(
        json.dumps(
            {
                "candidate_id": "donor-1",
                "pieces": [{"remote_media": source_recording}],
            }
        ),
        encoding="utf-8",
    )
    target_spec = tmp_path / "target-spec.json"
    target_spec.write_text(
        json.dumps(
            {
                "candidate_id": "target-1",
                "pieces": [{"remote_media": source_recording}],
            }
        ),
        encoding="utf-8",
    )
    reference_rows = []
    reference_hashes = {}
    for index, reference_id in enumerate(("r1", "r2", "r3"), start=1):
        path = tmp_path / f"{reference_id}.wav"
        path.write_bytes(f"reference-{index}".encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        reference_hashes[reference_id] = digest
        reference_rows.append({"id": reference_id, "sha256": digest, "path": path})

    samples = [tmp_path / "sample-1.wav", tmp_path / "sample-2.wav"]
    samples[0].write_bytes(b"sample-one")
    samples[1].write_bytes(b"sample-two")
    document = _source_session_document()
    document.update(
        source_recording=source_recording,
        profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
        model_tree_sha256="b" * 64,
        reference_hashes=reference_hashes,
        allowed_targets=[
            {
                "candidate_id": "target-1",
                "media_path": str(target_media),
                "media_sha256": hashlib.sha256(target_media.read_bytes()).hexdigest(),
                "provenance_path": str(target_spec),
                "provenance_sha256": hashlib.sha256(target_spec.read_bytes()).hexdigest(),
            }
        ],
        donor={
            "candidate_id": "donor-1",
            "media_path": str(donor_media),
            "media_sha256": hashlib.sha256(donor_media.read_bytes()).hexdigest(),
            "text_srt_path": str(donor_srt),
            "text_srt_sha256": hashlib.sha256(donor_srt.read_bytes()).hexdigest(),
            "provenance_path": str(donor_spec),
            "provenance_sha256": hashlib.sha256(donor_spec.read_bytes()).hexdigest(),
        },
        anchors=[
            {
                "source_cue": index,
                "start": f"00:00:0{1 if index == 1 else 4},000",
                "end": f"00:00:0{3 if index == 1 else 6},000",
                "text": f"anchor {'one' if index == 1 else 'two'}",
                "sample_sha256": hashlib.sha256(samples[index - 1].read_bytes()).hexdigest(),
                "reference_scores": {"r1": 0.72, "r2": 0.74, "r3": 0.76},
                "enroll_median_score": 0.74,
            }
            for index in (1, 2)
        ],
    )
    manifest = tmp_path / "session.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    def fake_extract(_media, _cues, work_dir):
        assert work_dir.is_dir()
        return None, 16_000, samples

    monkeypatch.setattr(
        "src.autoslice.speaker_finalizer._extract_cue_wavs", fake_extract
    )
    score_by_reference = {"r1.wav": 0.72, "r2.wav": 0.74, "r3.wav": 0.76}
    loaded, evidence = _load_source_session_anchor_samples(
        manifest,
        target_media_path=target_media,
        profile_path=profile,
        references=reference_rows,
        model_tree_sha256="b" * 64,
        host_seed_min=0.68,
        work_dir=tmp_path / "work",
        similarity=lambda left, _right: score_by_reference[left.name],
    )
    assert loaded == samples
    assert evidence["source_recording"] == source_recording
    assert evidence["target_candidate_id"] == "target-1"


def test_source_session_loader_reextracts_hash_bound_source_recording_segments(
    tmp_path: Path, monkeypatch,
) -> None:
    source_recording = tmp_path / "source.mp4"
    source_recording.write_bytes(b"source")
    canonical_target_media = tmp_path / "canonical-target.mp4"
    canonical_target_media.write_bytes(b"target")
    target_media = tmp_path / "runtime-copy.mp4"
    target_media.write_bytes(canonical_target_media.read_bytes())
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    target_spec = tmp_path / "target-spec.json"
    target_spec.write_text(
        json.dumps(
            {
                "candidate_id": "target-1",
                "pieces": [{"remote_media": str(source_recording)}],
            }
        ),
        encoding="utf-8",
    )
    reference_rows = []
    reference_hashes = {}
    for reference_id in ("r1", "r2", "r3"):
        path = tmp_path / f"{reference_id}.wav"
        path.write_bytes(reference_id.encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        reference_hashes[reference_id] = digest
        reference_rows.append({"id": reference_id, "sha256": digest, "path": path})
    sample_payloads = {1_000: b"segment-one", 5_000: b"segment-two"}
    anchors = []
    for start_ms, payload in sample_payloads.items():
        anchors.append(
            {
                "anchor_type": "source_recording_segment",
                "source_start_ms": start_ms,
                "source_end_ms": start_ms + 2_000,
                "text": f"segment {start_ms}",
                "sample_sha256": hashlib.sha256(payload).hexdigest(),
                "reference_scores": {"r1": 0.72, "r2": 0.74, "r3": 0.76},
                "enroll_median_score": 0.74,
            }
        )
    document = {
        "schema_version": SOURCE_SESSION_ANCHOR_SCHEMA,
        "status": "READY",
        "subject": "李豆沙",
        "source_session_id": "session-1",
        "source_recording": str(source_recording),
        "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
        "model_tree_sha256": "b" * 64,
        "reference_hashes": reference_hashes,
        "allowed_targets": [
            {
                "candidate_id": "target-1",
                "media_path": str(canonical_target_media),
                "media_sha256": hashlib.sha256(canonical_target_media.read_bytes()).hexdigest(),
                "provenance_path": str(target_spec),
                "provenance_sha256": hashlib.sha256(target_spec.read_bytes()).hexdigest(),
            }
        ],
        "anchors": anchors,
    }
    manifest = tmp_path / "segments.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    def fake_extract(_source, *, start_ms, expected_duration_ms, output_path):
        assert expected_duration_ms == 2_000
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(sample_payloads[start_ms])

    monkeypatch.setattr(
        "src.autoslice.speaker_finalizer._extract_checkpoint", fake_extract
    )
    scores = {"r1.wav": 0.72, "r2.wav": 0.74, "r3.wav": 0.76}
    loaded, evidence = _load_source_session_anchor_samples(
        manifest,
        target_media_path=target_media,
        profile_path=profile,
        references=reference_rows,
        model_tree_sha256="b" * 64,
        host_seed_min=0.68,
        work_dir=tmp_path / "work",
        similarity=lambda left, _right: scores[left.name],
    )
    assert len(loaded) == 2
    assert [row["anchor_type"] for row in evidence["anchors"]] == [
        "source_recording_segment",
        "source_recording_segment",
    ]

    mutated = False

    def drift_canonical_target(left, _right):
        nonlocal mutated
        if not mutated:
            canonical_target_media.write_bytes(b"drifted")
            mutated = True
        return scores[left.name]

    with pytest.raises(
        SpeakerFinalizationError,
        match="canonical target media drifted during analysis",
    ):
        _load_source_session_anchor_samples(
            manifest,
            target_media_path=target_media,
            profile_path=profile,
            references=reference_rows,
            model_tree_sha256="b" * 64,
            host_seed_min=0.68,
            work_dir=tmp_path / "drift-work",
            similarity=drift_canonical_target,
        )


def test_runner_surfaces_blocked_manifest_reason_before_runtime_warnings(
    tmp_path: Path, monkeypatch,
) -> None:
    output_manifest = tmp_path / "speaker.json"
    (tmp_path / "media.mp4").write_bytes(b"media")
    (tmp_path / "text.srt").write_text("text", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        output_manifest.write_text(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "production_ready": False,
                    "reason": "SpeakerFinalizationError: not enough Li Dousha clip anchors: []",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], 3, stdout="", stderr="ModelScope warning noise")

    monkeypatch.setattr("scripts.produce_slice_package.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="not enough Li Dousha clip anchors"):
        run_speaker_finalizer(
            host="localhost",
            candidate_id="candidate",
            media_path=tmp_path / "media.mp4",
            text_srt_path=tmp_path / "text.srt",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=output_manifest,
            work_dir=tmp_path / "work",
            speaker_python=tmp_path / "python",
        )

def test_context_prompt_treats_exact_shadow_name_as_lidousha_not_fourth_speaker() -> None:
    prompt = _context_prompt([], [], [])
    assert "精确词 shadow 是李豆沙的自称之一" in prompt
    assert "不是第四位说话人" in prompt


def test_speaker_context_loads_private_runtime_cpa_env(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "cpa.env"
    env_file.write_text(
        "export CPA_BASE_URL='http://127.0.0.1:8317/v1'\nexport CPA_API_KEY='secret-test-value'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_CPA_ENV", str(env_file))
    env = _speaker_context_env()
    assert env["CPA_BASE_URL"] == "http://127.0.0.1:8317/v1"
    assert env["CPA_API_KEY"] == "secret-test-value"


def test_ambiguous_speaker_resolution_records_context_and_fallback_sources() -> None:
    labels, sources = resolve_ambiguous_labels(
        ["连线", None, "李豆沙", None, "李豆沙"],
        [-0.3, -0.02, 0.3, 0.01, 0.4],
        0.0,
        {1: "李豆沙"},
    )
    assert labels == ["连线", "李豆沙", "李豆沙", "李豆沙", "李豆沙"]
    assert sources[1] == "whole_clip_context"
    assert sources[3] == "neighbour_context_fallback"


def test_finalizer_binds_text_before_speaker_and_renders_colour_without_prefixes(tmp_path: Path) -> None:
    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n她想问是三个位置哦\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n结果还是聋人啊\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    reference_dir = tmp_path / "refs"
    model_dir = tmp_path / "model"
    reference_dir.mkdir()
    model_dir.mkdir()

    def fake_analyzer(**kwargs):
        assert [cue.text for cue in kwargs["cues"]] == ["她想问是三个位置哦", "结果还是聋人啊"]
        return {
            "mode": "multi_speaker",
            "multi_speaker_detected": True,
            "decisions": [
                {"speaker": "连线", "decision_source": "campp_audio", "margin": -0.4},
                {"speaker": "连线", "decision_source": "whole_clip_context", "margin": 0.01},
            ],
        }

    output_srt = tmp_path / "speaker-final.srt"
    output_ass = tmp_path / "speaker-final.ass"
    output_manifest = tmp_path / "speaker-final.json"
    manifest = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=reference_dir,
        model_dir=model_dir,
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=output_manifest,
        work_dir=tmp_path / "work",
        analyzer=fake_analyzer,
    )

    assert "[连线] 她想问是三个位置哦" in output_srt.read_text(encoding="utf-8")
    ass = output_ass.read_text(encoding="utf-8")
    assert "Style: LDS" in ass and "Style: GUEST" in ass
    assert "[连线]" not in ass and "[李豆沙]" not in ass
    assert "Dialogue: 0,0:00:02.00,0:00:04.00,GUEST" in ass
    assert manifest["stage_order"] == "text_final_then_speaker_then_ass_then_burn"
    assert manifest["visible_speaker_prefixes"] is False
    assert manifest["subtitle_style"] == "lidousha-speaker-sapphire-host-white-guest-v2"
    assert manifest["speaker_taxonomy"] == "binary_visual_host_vs_guest"
    assert manifest["host_identity_aliases"] == ["李豆沙", "shadow"]
    assert json.loads(output_manifest.read_text(encoding="utf-8"))["production_ready"] is True


def test_reviewed_speaker_overrides_reject_automatic_label_drift_even_when_text_matches(
    tmp_path: Path,
) -> None:
    import hashlib
    import pytest

    from src.autoslice.speaker_finalizer import SpeakerFinalizationError

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n结果还是聋人啊\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": "test_candidate",
                "source_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "source_srt_sha256": hashlib.sha256(
                    "1\n00:00:00,000 --> 00:00:02,000\n[连线] 结果还是聋人啊\n".encode()
                ).hexdigest(),
                "overrides": [
                    {
                        "source_cue": 1,
                        "expect": {
                            "start": "00:00:00,000",
                            "end": "00:00:02,000",
                            "text": "结果还是聋人啊",
                        },
                        "authority": "Ivan direct correction",
                        "segments": [
                            {
                                "start": "00:00:00,000",
                                "end": "00:00:02,000",
                                "speaker": "连线",
                                "speaker_detail": "礼墨/Sumi",
                                "text": "结果还是聋人啊",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def wrong_auto(**_kwargs):
        return {
            "decisions": [{"speaker": "李豆沙", "decision_source": "acoustic_threshold_fallback", "margin": 0.5}],
            "context_unresolved_cues": [1],
        }

    with pytest.raises(SpeakerFinalizationError, match="source hash mismatch"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            candidate_id="test_candidate",
            override_path=overrides,
            analyzer=wrong_auto,
        )


def test_reviewed_context_votes_are_hash_bound_and_passed_to_analyzer(tmp_path: Path) -> None:
    import hashlib

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n嘿嘿嘿\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    accepted_automatic = (
        "1\n00:00:00,000 --> 00:00:02,000\n[连线] 嘿嘿嘿\n".encode()
    )
    accepted_hash = hashlib.sha256(accepted_automatic).hexdigest()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": "test_candidate",
                "source_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "source_srt_sha256": accepted_hash,
                "reviewed_context_votes": {
                    "authority": "accepted review fixture",
                    "source_automatic_srt_sha256": accepted_hash,
                    "labels": {"1": "连线"},
                },
                "overrides": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def analyzer(**kwargs):
        assert kwargs["reviewed_context_votes"] == {0: "连线"}
        return {
            "decisions": [
                {"speaker": "连线", "decision_source": "accepted_context_baseline", "margin": -0.1}
            ],
            "context_unresolved_cues": [],
        }

    output_srt = tmp_path / "speaker.srt"
    manifest = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "refs",
        model_dir=tmp_path / "model",
        output_srt_path=output_srt,
        output_ass_path=tmp_path / "speaker.ass",
        output_manifest_path=tmp_path / "speaker.json",
        work_dir=tmp_path / "work",
        candidate_id="test_candidate",
        override_path=overrides,
        analyzer=analyzer,
    )
    assert output_srt.read_bytes() == accepted_automatic
    assert manifest["automatic_labelled_srt_sha256"] == accepted_hash
    assert manifest["reviewed_output_cue_count"] == 0
    assert manifest["accepted_context_output_cue_count"] == 1


def test_reviewed_speaker_override_rejects_media_drift(tmp_path: Path) -> None:
    import hashlib
    import pytest

    from src.autoslice.speaker_finalizer import SpeakerFinalizationError

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"different media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n结果还是聋人啊\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_media_sha256": hashlib.sha256(b"original media").hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "overrides": [],
            }
        ),
        encoding="utf-8",
    )

    def analyzer(**_kwargs):
        return {"decisions": [{"speaker": "连线", "decision_source": "campp_audio"}]}

    with pytest.raises(SpeakerFinalizationError, match="media hash mismatch"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            override_path=overrides,
            analyzer=analyzer,
        )

    document = json.loads(overrides.read_text(encoding="utf-8"))
    document.pop("source_media_sha256")
    overrides.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SpeakerFinalizationError, match="missing source_media_sha256"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            override_path=overrides,
            analyzer=analyzer,
        )


def test_production_candidate_rejects_cross_candidate_speaker_override(tmp_path: Path) -> None:
    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nhello\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    automatic = "1\n00:00:00,000 --> 00:00:02,000\n[连线] hello\n"
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": "different_candidate",
                "source_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "source_srt_sha256": hashlib.sha256(automatic.encode()).hexdigest(),
                "overrides": [
                    {
                        "source_cue": 1,
                        "expect": {
                            "start": "00:00:00,000",
                            "end": "00:00:02,000",
                            "text": "hello",
                        },
                        "authority": "wrong candidate fixture",
                        "segments": [
                            {
                                "start": "00:00:00,000",
                                "end": "00:00:02,000",
                                "speaker": "李豆沙",
                                "text": "hello",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def analyzer(**_kwargs):
        return {
            "decisions": [{"speaker": "连线", "decision_source": "campp_audio"}],
            "context_unresolved_cues": [],
        }

    with pytest.raises(SpeakerFinalizationError, match="candidate_id mismatch"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            candidate_id="intended_candidate",
            override_path=overrides,
            analyzer=analyzer,
        )

    document = json.loads(overrides.read_text(encoding="utf-8"))
    document["candidate_id"] = "intended_candidate"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SpeakerFinalizationError, match="candidate_id is required"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work-omitted-candidate",
            override_path=overrides,
            analyzer=analyzer,
        )

def test_unanswered_ambiguous_context_blocks_production(tmp_path: Path) -> None:
    import pytest

    from src.autoslice.speaker_finalizer import SpeakerFinalizationError

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n为什么\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()

    def incomplete_analyzer(**_kwargs):
        return {
            "decisions": [{"speaker": "李豆沙", "decision_source": "acoustic_threshold_fallback"}],
            "context_unresolved_cues": [1],
        }

    with pytest.raises(SpeakerFinalizationError, match="did not resolve ambiguous speaker cues: 1"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            analyzer=incomplete_analyzer,
        )
