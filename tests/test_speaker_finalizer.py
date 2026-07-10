import json
from pathlib import Path

from src.autoslice.speaker_finalizer import (
    _context_prompt,
    _pair_cache_key,
    _speaker_context_env,
    finalize_speaker_subtitles,
    resolve_ambiguous_labels,
)


def test_pair_cache_key_is_symmetric_and_model_bound() -> None:
    assert _pair_cache_key("model-a", "left", "right") == _pair_cache_key("model-a", "right", "left")
    assert _pair_cache_key("model-a", "left", "right") != _pair_cache_key("model-b", "left", "right")


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
